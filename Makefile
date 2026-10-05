# Root Makefile. Each required check has a target named exactly like its check id, and CI
# runs `make <check-id>` as the last step of that job, so local and CI runs are the same.
SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

# Per-checkout host ports (ADR-0011 amendment):
# `.worktree.env` is optional and git-ignored; the helper validates it before make reads it.
WORKTREE_KEYS := COMPOSE_PROJECT_NAME PG_HOST_PORT MINIO_API_HOST_PORT MINIO_CONSOLE_HOST_PORT API_HOST_PORT
ifneq ($(wildcard .worktree.env),)
ifneq ($(shell scripts/ci/worktree_env.sh exports .worktree.env >/dev/null 2>&1; echo $$?),0)
$(error .worktree.env rejected: $(shell scripts/ci/worktree_env.sh exports .worktree.env 2>&1 >/dev/null))
endif
-include .worktree.env
endif
$(foreach k,$(WORKTREE_KEYS),$(if $(and $(filter file,$(origin $(k))),$(strip $($(k)))),$(eval export $(k))))
PG_HOST_PORT := $(or $(PG_HOST_PORT),5432)
API_HOST_PORT := $(or $(API_HOST_PORT),8000)

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
	@echo "make dev | hooks | test-unit | bootstrap-db | test-db | <check-id>"

.PHONY: test-unit
test-unit:
	uv run --frozen pytest -q apps/api/tests/unit apps/api/tests/meta packages/schemas/tests

# ---- required checks (one target per check id) ----
LINT_PATHS := apps/api packages/schemas scripts

.PHONY: api-lint
api-lint:
	uv run --frozen ruff check $(LINT_PATHS)
	uv run --frozen black --check $(LINT_PATHS)
	uv run --frozen python scripts/ci/check_repo_rules.py
	uv run --frozen python scripts/ci/run_pip_audit.py

# import-linter, then the tenant_scope_checked grep (CANON-ADDENDUM.md §6): the literal may appear
# only in apps/api/app/db/{guard,session}.py. Zero hits pass until S9 adds them.
.PHONY: lint-imports
lint-imports:
	uv run --frozen lint-imports --config .importlinter
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
.PHONY: bootstrap-db
bootstrap-db:
	$(PSQL) -d marketplace_os -f - < scripts/db/0000_bootstrap.sql
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

# ---- tests (CANON.md:136-137; the CI env block is these three variables, CANON.md:21) ----
TEST_DATABASE_URL ?= postgresql+psycopg://postgres:postgres@localhost:$(PG_HOST_PORT)/mos_test
ISO_DATABASE_URL ?= postgresql+psycopg://postgres:postgres@localhost:$(PG_HOST_PORT)/mos_iso
CI_JWT_SECRET := ci_only_not_a_real_secret_at_least_32_chars
COV_FAIL_UNDER ?= 70

# The isolation sentinel asserts mos_iso, so it runs only in its own job (ADR-0013 §4, until S9).
.PHONY: api-tests
api-tests:
	$(PG_PORT_GUARD)
	APP_ENV=local DATABASE_URL=$(TEST_DATABASE_URL) JWT_SECRET=$(CI_JWT_SECRET) \
	  uv run --frozen pytest -q -m "not tenant_isolation and not cloudsql_mimic" --cov=app --cov-report=term-missing \
	  --cov-fail-under=$(COV_FAIL_UNDER)

.PHONY: tenant-isolation
tenant-isolation:
	$(PG_PORT_GUARD)
	APP_ENV=local DATABASE_URL=$(ISO_DATABASE_URL) JWT_SECRET=$(CI_JWT_SECRET) \
	  uv run --frozen pytest -q -m tenant_isolation --no-cov

# 0000 (and the password file) as a Cloud SQL-style non-superuser CREATEROLE role, on a throwaway
# container from the compose postgres image (DATABASE.md:1754). Docker only; no database URL.
.PHONY: bootstrap-mimic
bootstrap-mimic:
	uv run --frozen pytest -q -m cloudsql_mimic --no-cov apps/api/tests/cloudsql_mimic

# ---- local development (CANON-ADDENDUM.md §2 order; Alembic joins in S5) ----
# uvicorn keeps its 127.0.0.1 default: never pass --host here.
.PHONY: dev
dev:
	scripts/ci/worktree_env.sh check
	docker compose up -d --wait postgres minio
	docker compose up -d minio-init
	$(MAKE) bootstrap-db
	uv sync --frozen --all-packages
	$(MAKE) hooks
	uv run --frozen uvicorn app.main:app --reload --port $(API_HOST_PORT) --app-dir apps/api

.PHONY: hooks
hooks:
	uv run --frozen pre-commit install

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

# The generators land in S2/S3; the drift check also fails on untracked generated files.
.PHONY: schemas-drift
schemas-drift:
	$(MAKE) -C packages/schemas all
	git diff --exit-code -- packages/schemas/jsonschema packages/schemas/ts
	@untracked="$$(git ls-files --others --exclude-standard -- packages/schemas/jsonschema packages/schemas/ts)"; \
	if [ -n "$$untracked" ]; then echo "schemas-drift: untracked generated files:"; echo "$$untracked"; exit 1; fi; \
	echo "schemas-drift: OK"
