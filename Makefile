# Root Makefile. Each required check has a target named exactly like its check id, and CI
# runs `make <check-id>` as the last step of that job, so local and CI runs are the same.
SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "make dev | hooks | test-unit | bootstrap-db | test-db | <check-id>"

.PHONY: test-unit
test-unit:
	uv run --frozen pytest -q apps/api/tests/unit apps/api/tests/meta

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
PSQL := docker compose exec -T postgres psql -U postgres -v ON_ERROR_STOP=1
BOOTSTRAP_SQL := /docker-entrypoint-initdb.d/0000_bootstrap.sql

.PHONY: bootstrap-db
bootstrap-db:
	$(PSQL) -d marketplace_os -f $(BOOTSTRAP_SQL)

# The test databases: mos_test for api-tests, mos_iso for tenant-isolation (never marketplace_os).
.PHONY: test-db
test-db:
	for db in mos_test mos_iso; do \
	  if ! $(PSQL) -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$$db'" | grep -qx 1; then \
	    docker compose exec -T postgres createdb -U postgres "$$db"; \
	  fi; \
	  $(PSQL) -d "$$db" -f $(BOOTSTRAP_SQL); \
	done

# ---- tests (CANON.md:136-137; the CI env block is these three variables, CANON.md:21) ----
TEST_DATABASE_URL ?= postgresql+psycopg://postgres:postgres@localhost:5432/mos_test
ISO_DATABASE_URL ?= postgresql+psycopg://postgres:postgres@localhost:5432/mos_iso
CI_JWT_SECRET := ci_only_not_a_real_secret_at_least_32_chars
COV_FAIL_UNDER ?= 70

# The isolation sentinel asserts mos_iso, so it runs only in its own job (ADR-0013 §4, until S9).
.PHONY: api-tests
api-tests:
	APP_ENV=local DATABASE_URL=$(TEST_DATABASE_URL) JWT_SECRET=$(CI_JWT_SECRET) \
	  uv run --frozen pytest -q -m "not tenant_isolation" --cov=app --cov-report=term-missing \
	  --cov-fail-under=$(COV_FAIL_UNDER)

.PHONY: tenant-isolation
tenant-isolation:
	APP_ENV=local DATABASE_URL=$(ISO_DATABASE_URL) JWT_SECRET=$(CI_JWT_SECRET) \
	  uv run --frozen pytest -q -m tenant_isolation --no-cov

# ---- local development (CANON-ADDENDUM.md §2 order; Alembic joins in S5) ----
# uvicorn keeps its 127.0.0.1 default: never pass --host here.
.PHONY: dev
dev:
	docker compose up -d --wait postgres minio
	docker compose up -d minio-init
	$(MAKE) bootstrap-db
	uv sync --frozen --all-packages
	$(MAKE) hooks
	uv run --frozen uvicorn app.main:app --reload --port 8000 --app-dir apps/api

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
