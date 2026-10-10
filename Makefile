# Root Makefile. Each required check has a target named exactly like its check id, and CI
# runs `make <check-id>` as the last step of that job, so local and CI runs are the same.
SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

# Per-checkout host ports (ADR-0011 amendment):
# `.worktree.env` is optional and git-ignored; the helper validates it before make reads it.
WORKTREE_KEYS := COMPOSE_PROJECT_NAME PG_HOST_PORT MINIO_API_HOST_PORT MINIO_CONSOLE_HOST_PORT API_HOST_PORT MOCK_HOST_PORT FAKE_MARKETPLACE_HOST_PORT FLAG_OFF_API_HOST_PORT
# `make e2e` against a caller's own stack (S13b): an API_HOST_PORT in the environment wins over
# .worktree.env there (read before the file is included).
E2E_ENV_API_PORT := $(if $(filter environment,$(origin API_HOST_PORT)),$(API_HOST_PORT))
ifneq ($(wildcard .worktree.env),)
ifneq ($(shell scripts/ci/worktree_env.sh exports .worktree.env >/dev/null 2>&1; echo $$?),0)
$(error .worktree.env rejected: $(shell scripts/ci/worktree_env.sh exports .worktree.env 2>&1 >/dev/null))
endif
-include .worktree.env
endif
$(foreach k,$(WORKTREE_KEYS),$(if $(and $(filter file,$(origin $(k))),$(strip $($(k)))),$(eval export $(k))))
PG_HOST_PORT := $(or $(PG_HOST_PORT),5432)
API_HOST_PORT := $(or $(API_HOST_PORT),8000)
MOCK_HOST_PORT := $(or $(MOCK_HOST_PORT),4010)
FAKE_MARKETPLACE_HOST_PORT := $(or $(FAKE_MARKETPLACE_HOST_PORT),4020)
FLAG_OFF_API_HOST_PORT := $(or $(FLAG_OFF_API_HOST_PORT),8011)

# A reason when $(1) is not one all-digit word in 1..65535. Non-digits never reach the shell.
_nondigit = $(subst 0,,$(subst 1,,$(subst 2,,$(subst 3,,$(subst 4,,$(subst 5,,$(subst 6,,$(subst 7,,$(subst 8,,$(subst 9,,$(1)))))))))))
port_problem = $(if $(filter-out 1,$(words $(1))),not one word,$(if $(call _nondigit,$(1)),not digits,$(if $(filter 0%,$(1)),leading zero,$(if $(filter ok,$(shell [ $(1) -le 65535 ] && echo ok)),,above 65535))))

pg_port_problem := $(call port_problem,$(PG_HOST_PORT))
ifneq ($(pg_port_problem),)
$(error PG_HOST_PORT is not a valid port: $(pg_port_problem))
endif
api_port_problem := $(call port_problem,$(API_HOST_PORT))
ifneq ($(api_port_problem),)
$(error API_HOST_PORT is not a valid port: $(api_port_problem))
endif
mock_port_problem := $(call port_problem,$(MOCK_HOST_PORT))
ifneq ($(mock_port_problem),)
$(error MOCK_HOST_PORT is not a valid port: $(mock_port_problem))
endif
fake_port_problem := $(call port_problem,$(FAKE_MARKETPLACE_HOST_PORT))
ifneq ($(fake_port_problem),)
$(error FAKE_MARKETPLACE_HOST_PORT is not a valid port: $(fake_port_problem))
endif
flag_off_port_problem := $(call port_problem,$(FLAG_OFF_API_HOST_PORT))
ifneq ($(flag_off_port_problem),)
$(error FLAG_OFF_API_HOST_PORT is not a valid port: $(flag_off_port_problem))
endif
minio_api_port_problem := $(if $(strip $(MINIO_API_HOST_PORT)),$(call port_problem,$(MINIO_API_HOST_PORT)))
ifneq ($(minio_api_port_problem),)
$(error MINIO_API_HOST_PORT is not a valid port: $(minio_api_port_problem))
endif
minio_console_port_problem := $(if $(strip $(MINIO_CONSOLE_HOST_PORT)),$(call port_problem,$(MINIO_CONSOLE_HOST_PORT)))
ifneq ($(minio_console_port_problem),)
$(error MINIO_CONSOLE_HOST_PORT is not a valid port: $(minio_console_port_problem))
endif

ifeq ($(CI),true)
PG_PORT_GUARD := @:
else
PG_PORT_GUARD = @published="$$(docker compose port postgres 5432 2>/dev/null)"; \
	[ "$$published" = "127.0.0.1:$(PG_HOST_PORT)" ] || { echo "make: this checkout's compose postgres is on '$$published', want 127.0.0.1:$(PG_HOST_PORT)" >&2; exit 1; }
endif

.PHONY: help
help:
	@echo "make dev | worker | hooks | test-unit | bootstrap-db | test-db | mock | mock-smoke | seed | e2e | <check-id>"

.PHONY: test-unit
test-unit:
	uv run --frozen pytest -q apps/api/tests/unit apps/api/tests/meta packages/schemas/tests packages/pipeline-stub/tests

# ---- required checks (one target per check id) ----
LINT_PATHS := apps/api packages/schemas packages/pipeline-stub scripts

.PHONY: api-lint
api-lint:
	uv run --frozen ruff check $(LINT_PATHS)
	uv run --frozen black --check $(LINT_PATHS)
	uv run --frozen python scripts/ci/check_repo_rules.py
	uv run --frozen python scripts/ci/run_pip_audit.py

# import-linter, repo rules R5-R8 (platform_scope, SCOPE_OK, identity_scope, the job GUC), then the tenant_scope_checked grep (CANON-ADDENDUM.md §6): the literal may appear
# only in apps/api/app/db/{guard,session}.py. Zero hits pass until S9 adds them.
.PHONY: lint-imports
lint-imports:
	PYTHONPATH=apps/api uv run --frozen lint-imports --config .importlinter
	uv run --frozen python scripts/ci/check_repo_rules.py --rules R5,R6,R7,R8
	@hits="$$(grep -rl --include='*.py' tenant_scope_checked apps packages scripts)" || [ $$? -eq 1 ]; \
	bad=0; \
	for hit in $$hits; do \
	  case "$$hit" in \
	    apps/api/app/db/guard.py|apps/api/app/db/session.py) ;; \
	    *) echo "$$hit: tenant_scope_checked outside apps/api/app/db/{guard,session}.py"; bad=1 ;; \
	  esac; \
	done; \
	if [ "$$bad" -ne 0 ]; then exit 1; fi; \
	echo "tenant_scope_checked grep: OK"

# ---- local database (ADR-0013 §7: psql runs inside the compose container, no host psql) ----
# The SQL files go on stdin from the repo, never through the initdb bind mount, so an edit that
# replaces a file's inode never leaves the container on a stale copy.
PSQL := docker compose exec -T postgres psql -U postgres -v ON_ERROR_STOP=1

# Role passwords: SCRAM verifiers only (scripts/db/scram_verifier.py), passed by name from the
# caller's environment; unset or empty skips that role. Never `NAME=value` on this line.
# The local app logs in as app_rw and Alembic as migrator (ENV.md DATABASE_URL and
# ALEMBIC_DATABASE_URL, CODE-PLAN S10 Q5): unless the caller passes its own verifiers, the
# local dev-only passwords below are set (the same constants .env.example names; local and CI
# clusters only, never a real secret).
.PHONY: bootstrap-db
bootstrap-db:
	$(PSQL) -d marketplace_os -f - < scripts/db/0000_bootstrap.sql
	APP_RW_SCRAM="$${APP_RW_SCRAM:-$$($(APP_RW_SCRAM_CMD))}" \
	  MIGRATOR_SCRAM="$${MIGRATOR_SCRAM:-$$($(MIGRATOR_SCRAM_CMD))}" \
	  docker compose exec -T -e APP_RW_SCRAM -e APP_RO_SCRAM -e MIGRATOR_SCRAM postgres \
	  psql -U postgres -v ON_ERROR_STOP=1 -X -q -d marketplace_os -f - < scripts/db/0000_role_passwords.sql

# The test databases: mos_test for api-tests, mos_iso for tenant-isolation (never marketplace_os).
.PHONY: test-db
test-db:
	$(PG_PORT_GUARD)
	for db in mos_test mos_iso; do \
	  if ! $(PSQL) -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$$db'" | grep -qx 1; then \
	    docker compose exec -T postgres createdb -U postgres "$$db"; \
	  fi; \
	  $(PSQL) -d "$$db" -f - < scripts/db/0000_bootstrap.sql; \
	done
	APP_RW_SCRAM="$$($(APP_RW_SCRAM_CMD))" \
	  docker compose exec -T -e APP_RW_SCRAM postgres \
	  psql -U postgres -v ON_ERROR_STOP=1 -X -q -d mos_iso -f - < scripts/db/0000_role_passwords.sql

# The tenant-isolation suite logs in as app_rw (S9 D7, D14). Its CI-only password is this constant,
# usable only against the local and CI test clusters; the helper reads it on stdin (printf is a
# shell builtin, so it never sits on a process argv) and prints the SCRAM verifier.
.PHONY: app-rw-scram
app-rw-scram:
	@$(APP_RW_SCRAM_CMD)

# ---- tests (CANON.md:136-137; the CI env block is these three variables, CANON.md:21) ----
TEST_DATABASE_URL ?= postgresql+psycopg://postgres:postgres@localhost:$(PG_HOST_PORT)/mos_test
# The compose MinIO (the CSV import tests, S13b); the bucket names and keys are the settings' defaults.
TEST_S3_ENDPOINT_URL ?= http://localhost:$(or $(MINIO_API_HOST_PORT),9000)
CI_JWT_SECRET := ci_only_not_a_real_secret_at_least_32_chars
CI_APP_RW_PASSWORD := ci_only_app_rw_not_a_real_password
CI_MIGRATOR_PASSWORD := ci_only_migrator_not_a_real_password
# tenant-isolation: the admin login migrates mos_iso and builds fixtures; the suite logs in as app_rw.
ISO_ADMIN_URL ?= postgresql+psycopg://postgres:postgres@localhost:$(PG_HOST_PORT)/mos_iso
ISO_DATABASE_URL ?= postgresql+psycopg://app_rw:$(CI_APP_RW_PASSWORD)@localhost:$(PG_HOST_PORT)/mos_iso
APP_RW_SCRAM_CMD = printf '%s' '$(CI_APP_RW_PASSWORD)' | uv run --frozen python scripts/db/scram_verifier.py
MIGRATOR_SCRAM_CMD = printf '%s' '$(CI_MIGRATOR_PASSWORD)' | uv run --frozen python scripts/db/scram_verifier.py
COV_FAIL_UNDER ?= 70
# api-tests runs pytest under pytest-xdist: `auto` workers, never more than 4 (--maxprocesses).
# Each worker gwN copies the migrated, synced mos_test to mos_test_gwN (tests/support/xdist_db.py).
API_TEST_WORKERS ?= auto

# Alembic on mos_test before pytest (CANON.md:136): upgrade, check, downgrade, upgrade, then squawk
# over the rendered SQL, report-only until the first prod tag (EXECUTION-PLAN.md:303-305). Then the
# attribute dictionary sync (DATABASE.md:1785), which the facts guard reads at write time. env.py
# switches the postgres login to migrator. The CI env block stays three variables: the URL is here.
ALEMBIC := uv run --frozen alembic -c apps/api/alembic.ini
MIGRATE_ENV = APP_ENV=local DATABASE_URL=$(TEST_DATABASE_URL) ALEMBIC_DATABASE_URL=$(TEST_DATABASE_URL) JWT_SECRET=$(CI_JWT_SECRET)

# api-tests keeps `not tenant_isolation`: that suite needs app_rw on a migrated mos_iso (S9 D8).
.PHONY: api-tests
api-tests:
	$(PG_PORT_GUARD)
	$(MIGRATE_ENV) $(ALEMBIC) upgrade head
	$(MIGRATE_ENV) $(ALEMBIC) check
	$(MIGRATE_ENV) $(ALEMBIC) downgrade base
	$(MIGRATE_ENV) $(ALEMBIC) upgrade head
	sql="$$(mktemp)"; trap 'rm -f "$$sql"' EXIT; \
	  $(MIGRATE_ENV) $(ALEMBIC) upgrade head --sql >"$$sql"; \
	  uv run --frozen python scripts/ci/squawk_report.py "$$sql"
	$(MIGRATE_ENV) uv run --frozen python -m app.cli dictionary sync
	APP_ENV=local DATABASE_URL=$(TEST_DATABASE_URL) JWT_SECRET=$(CI_JWT_SECRET) S3_ENDPOINT_URL=$(TEST_S3_ENDPOINT_URL) \
	  uv run --frozen pytest -q -m "not tenant_isolation and not cloudsql_mimic" \
	  -n $(API_TEST_WORKERS) --maxprocesses=4 --dist loadgroup --cov=app --cov-report=term-missing \
	  --cov-fail-under=$(COV_FAIL_UNDER)
	cov="$$(mktemp)"; trap 'rm -f "$$cov"' EXIT; \
	  uv run --frozen coverage json -q --include='*/app/services/rules_engine/*' -o "$$cov" && \
	  uv run --frozen python scripts/ci/branch_coverage.py "$$cov" --min 90 --label rules_engine

# Mutation run over the Rules Engine (ADR-0010 §2): report-only, not a required check (S14a T16).
.PHONY: mutation-rules
mutation-rules:
	cd apps/api && uv run --frozen mutmut run

.PHONY: tenant-isolation
tenant-isolation:
	$(PG_PORT_GUARD)
	APP_ENV=local APP_ROLE=migrate ALEMBIC_DATABASE_URL=$(ISO_ADMIN_URL) uv run --frozen python -m app.cli db migrate
	APP_ENV=local APP_ROLE=migrate ALEMBIC_DATABASE_URL=$(ISO_ADMIN_URL) uv run --frozen python -m app.cli dictionary sync
	APP_ENV=local DATABASE_URL=$(ISO_DATABASE_URL) ALEMBIC_DATABASE_URL=$(ISO_ADMIN_URL) JWT_SECRET=$(CI_JWT_SECRET) \
	  uv run --frozen pytest -q -m tenant_isolation --no-cov

# 0000 (and the password file) as a Cloud SQL-style non-superuser CREATEROLE role, on a throwaway
# container from the compose postgres image (DATABASE.md:1754). Docker only; no database URL.
.PHONY: bootstrap-mimic
bootstrap-mimic:
	uv run --frozen pytest -q -m cloudsql_mimic --no-cov apps/api/tests/cloudsql_mimic

# ---- local development (CANON-ADDENDUM.md §2 order: bootstrap-db, then Alembic) ----
# S15: the local API and the e2e refresh pass talk OAuth to the fake marketplace
# (`make fake-marketplace`) on this checkout's FAKE_MARKETPLACE_HOST_PORT, with the fake's
# placeholder SP-API application id. The client ids and secrets are the .env.example placeholders.
# S21: the SP-API endpoint is the fake too (a loopback host is allowed only at APP_ENV=local), so
# the e2e worker's publish and status-sync jobs never leave the machine.
# S22 C2: the local API (and every e2e hook) also runs with Flipkart on against the fake's listing
# routes; `.env.example` and the Settings default keep FLIPKART_ENABLED=false everywhere else.
FAKE_MARKETPLACE_URL = http://127.0.0.1:$(FAKE_MARKETPLACE_HOST_PORT)
FAKE_AMAZON_APP_ID := amzn1.sp.solution.REPLACE_ME
FAKE_MARKETPLACE_ENV = AMAZON_APP_ID=$(FAKE_AMAZON_APP_ID) \
	AMAZON_AUTHORIZE_URL=$(FAKE_MARKETPLACE_URL)/amazon/authorize \
	AMAZON_LWA_TOKEN_URL=$(FAKE_MARKETPLACE_URL)/amazon/token \
	FLIPKART_AUTHORIZE_URL=$(FAKE_MARKETPLACE_URL)/flipkart/authorize \
	FLIPKART_TOKEN_URL=$(FAKE_MARKETPLACE_URL)/flipkart/token \
	AMAZON_SPAPI_ENDPOINT=$(FAKE_MARKETPLACE_URL) \
	FLIPKART_API_BASE=$(FAKE_MARKETPLACE_URL)/flipkart/sellers \
	FLIPKART_ENABLED=true

# uvicorn keeps its 127.0.0.1 default: never pass --host here.
.PHONY: dev
dev:
	scripts/ci/worktree_env.sh check
	docker compose up -d --wait postgres minio
	docker compose up -d minio-init
	$(MAKE) bootstrap-db
	uv sync --frozen --all-packages
	uv run --frozen alembic -c apps/api/alembic.ini upgrade head
	uv run --frozen python -m app.cli dictionary sync
	$(MAKE) hooks
	$(FAKE_MARKETPLACE_ENV) uv run --frozen uvicorn app.main:app --reload --port $(API_HOST_PORT) --app-dir apps/api

# A local worker beside `make dev` (S13): both queues, the same env as the dev API (.env, as app_rw).
.PHONY: worker
worker:
	APP_ROLE=worker APP_NAME=marketplace-os-worker OTEL_SERVICE_NAME=marketplace-os-worker \
	  uv run --frozen python -m app.workers.main

# The local /api/v1 mock (S3 D5): committed openapi.json examples on 127.0.0.1 only. `Prefer:
# code=<status>` answers a declared error example. `mock-smoke` starts it on a free port and probes it.
# Tag CI only (publish-schemas.yml); refuses to run anywhere else. Publishing is the user's go.
.PHONY: publish-schemas
publish-schemas:
	scripts/ci/publish_schemas.sh

.PHONY: mock
mock:
	uv run --frozen python -m app_mock --host 127.0.0.1 --port $(MOCK_HOST_PORT)

# The local fake marketplace (S15): Amazon and Flipkart OAuth consent and token endpoints for the
# connect flow, on 127.0.0.1 only (the module binds loopback; never pass --host). Dev tooling under
# apps/api/tests/support/, never imported by the app and not in the image.
.PHONY: fake-marketplace
fake-marketplace:
	uv run --frozen python apps/api/tests/support/fake_marketplace.py --port $(FAKE_MARKETPLACE_HOST_PORT)

.PHONY: mock-smoke
mock-smoke:
	scripts/ci/mock_smoke.sh

.PHONY: hooks
hooks:
	uv run --frozen pre-commit install

# The local demo data (CANON-ADDENDUM.md A§5); the command refuses any APP_ENV but local.
.PHONY: seed
seed:
	uv run --frozen python -m app.cli seed --profile demo

# The local HTTP e2e scripts (CODE-PLAN S12 T14) against `make dev` with `make seed` done: every
# scripts/e2e/*.sh in order, each after the shared limiter reset, stopping at the first failure.
# The real limiter stays on (no RATE_LIMIT_* override); the variables exist for the meta test.
# `|| exit` because make 3.81 (macOS) ignores .SHELLFLAGS, so `-e` cannot be relied on here.
# S15: the fake marketplace runs for the whole pass (FAKE_CTL starts and stops it, PID file), and
# the scripts get its URL (FAKE_URL) and a one-refresh-pass command (REFRESH_ONCE, the worker's
# one-shot entry with the same env as `make dev`).
E2E_DIR ?= scripts/e2e
RESET_LIMITS ?= scripts/ci/reset_rate_limits.sh
FAKE_CTL ?= scripts/ci/fake_marketplace.sh
# S22 C2: a second, flag-off API (FLIPKART_ENABLED=false) on FLAG_OFF_API_HOST_PORT for the whole
# pass; the scripts get its URL as FLAG_OFF_URL.
FLAG_OFF_CTL ?= scripts/ci/flag_off_api.sh
REFRESH_ONCE ?= APP_ROLE=worker uv run --frozen python -m app.workers.oneshot refresh_marketplace_tokens

# The import flow's hooks (S13b): a local worker beside `make dev` (its PID in E2E_WORKER_PIDFILE)
# and the migrator's `templates load`. DATABASE_URL, ALEMBIC_DATABASE_URL and API_HOST_PORT set in
# the caller's environment win over .env and .worktree.env (a caller with its own stack).
E2E_WORKER_PIDFILE ?= $(CURDIR)/.e2e/worker.pid
E2E_WORKER_START = mkdir -p "$$(dirname "$(E2E_WORKER_PIDFILE)")"; \
  APP_ROLE=worker APP_NAME=marketplace-os-worker OTEL_SERVICE_NAME=marketplace-os-worker \
  nohup uv run --frozen python -m app.workers.main >"$$(dirname "$(E2E_WORKER_PIDFILE)")/worker.log" 2>&1 & \
  echo $$! >"$(E2E_WORKER_PIDFILE)"
E2E_WORKER_STOP = pid="$$(cat "$(E2E_WORKER_PIDFILE)")"; kill -TERM "$$pid"; \
  for _ in $$(seq 1 30); do kill -0 "$$pid" 2>/dev/null || break; sleep 1; done; \
  ! kill -0 "$$pid" 2>/dev/null && rm -f "$(E2E_WORKER_PIDFILE)"
E2E_TEMPLATES_LOAD = uv run --frozen python -m app.cli templates load --file "$$1"
# The marketplace preview e2e's hook (S20 T21): the migrator's `errors load`.
E2E_ERRORS_LOAD = uv run --frozen python -m app.cli errors load --file "$$1"
# S16: the staff CLI as migrator (ALEMBIC_DATABASE_URL from .env or the caller); the brand
# onboarding flow appends its arguments and gives a new user's password on stdin.
E2E_MKOS_CLI = uv run --frozen python -m app.cli

.PHONY: e2e
e2e:
	export WORKER_START='$(E2E_WORKER_START)' WORKER_STOP='$(E2E_WORKER_STOP)' \
	  TEMPLATES_LOAD='$(E2E_TEMPLATES_LOAD)' ERRORS_LOAD='$(E2E_ERRORS_LOAD)' MKOS_CLI='$(E2E_MKOS_CLI)'; \
	base="http://127.0.0.1:$(or $(E2E_ENV_API_PORT),$(API_HOST_PORT))"; \
	$(FAKE_CTL) start $(FAKE_MARKETPLACE_HOST_PORT) || exit $$?; \
	trap '$(FAKE_CTL) stop $(FAKE_MARKETPLACE_HOST_PORT)' EXIT; \
	export $(FAKE_MARKETPLACE_ENV); \
	export FAKE_URL="$(FAKE_MARKETPLACE_URL)" REFRESH_ONCE="$(REFRESH_ONCE)"; \
	$(FLAG_OFF_CTL) start $(FLAG_OFF_API_HOST_PORT) || exit $$?; \
	trap '$(FLAG_OFF_CTL) stop $(FLAG_OFF_API_HOST_PORT); $(FAKE_CTL) stop $(FAKE_MARKETPLACE_HOST_PORT)' EXIT; \
	export FLAG_OFF_URL="http://127.0.0.1:$(FLAG_OFF_API_HOST_PORT)"; \
	for script in $(E2E_DIR)/*.sh; do \
	  [ -e "$$script" ] || { echo "make e2e: no scripts in $(E2E_DIR)" >&2; exit 1; }; \
	  [ "$$(basename "$$script")" != onboarding.sh ] || continue; \
	  $(RESET_LIMITS) "$$base" || exit $$?; \
	  "$$script" "$$base" || exit $$?; \
	done

# ONBOARDING.md on a clean clone of this branch (S14b T25): its own compose project and free ports,
# every documented command, then the clone, its stack and its processes removed. Not a CI check.
.PHONY: e2e-onboarding
e2e-onboarding:
	scripts/e2e/onboarding.sh

.PHONY: bootstrap
bootstrap:
	scripts/ci/bootstrap.sh

# PR_TITLE and HEAD_REF come from the job's env block, never inline (EXECUTION-PLAN.md:245).
.PHONY: commit-lint
commit-lint:
	python3 scripts/ci/commit_lint.py --title "$$PR_TITLE" --branch "$$HEAD_REF"

.PHONY: gitleaks
gitleaks:
	scripts/ci/gitleaks.sh

.PHONY: api-typecheck
api-typecheck:
	uv run --frozen mypy

.PHONY: contracts
contracts:
	scripts/ci/contracts.sh

# Full strength since S3 (CANON.md:139): regenerate the JSON Schemas, openapi.json and the TS types,
# fail on any diff or untracked generated file, then prove the package packs only its generated
# files and type-checks for an FE consumer importing `@mkos/schemas`.
.PHONY: schemas-drift
schemas-drift:
	$(MAKE) -C packages/schemas all
	git diff --exit-code -- packages/schemas/jsonschema packages/schemas/ts
	@untracked="$$(git ls-files --others --exclude-standard -- packages/schemas/jsonschema packages/schemas/ts)"; \
	if [ -n "$$untracked" ]; then echo "schemas-drift: untracked generated files:"; echo "$$untracked"; exit 1; fi
	$(MAKE) -C packages/schemas ts-pack-check
	$(MAKE) -C packages/schemas ts-consumer-check
	@echo "schemas-drift: OK"
