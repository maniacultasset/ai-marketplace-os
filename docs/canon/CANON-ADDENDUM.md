# Canon Addendum — closing rulings

This addendum closes the six unresolved findings and all thirteen canon-versus-artifact conflicts in kick_verify.json. It is part of THE CANON and carries the identical authority: where it disagrees with a canon row, an artifact file, a build step or any draft section, this addendum wins and the other text is superseded on sight. Ordered by blast radius — bootstrap-blocking first, then CI-blocking, then contract/type drift, then ops and cosmetics — and every ruling below is a copy-pasteable value, path, filename or line, not an instruction to go and align things.


## 1. Bootstrap SQL — the one filename and the compose mount

The file is `scripts/db/0000_bootstrap.sql`, exactly that name, exactly that path. `scripts/db/00-extensions.sql` is DELETED from the repo and from every document. The docker-compose postgres service is rewritten to mount the new name; this is the whole service, verbatim (host port 5432 is the default; a second worktree overrides only the host port, per the ADR-0011 two-track amendment, user 2026-10-04):

```yaml
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: marketplace_os
    ports: ["5432:5432"]
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./scripts/db/0000_bootstrap.sql:/docker-entrypoint-initdb.d/0000_bootstrap.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d marketplace_os"]
      interval: 5s
      timeout: 5s
      retries: 10
```

The file MUST be idempotent, because initdb.d runs only on an empty data directory and `make bootstrap-db` re-runs it on existing volumes. Its full content:

```sql
-- scripts/db/0000_bootstrap.sql — superuser, idempotent, NOT an Alembic revision.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator')
  THEN CREATE ROLE migrator LOGIN; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw')
  THEN CREATE ROLE app_rw LOGIN; END IF; END $$;
ALTER SCHEMA public OWNER TO migrator;
GRANT USAGE ON SCHEMA public TO migrator, app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_rw;
```

> **Supersedes:** The `./scripts/db/00-extensions.sql:/docker-entrypoint-initdb.d/00-extensions.sql:ro` line in the pack's only docker-compose (kick_drafts sec0 code_block 1), and the `scripts/db/00-extensions.sql` comment at the top of sec0 code_block 2. Resolves kick_verify unresolved #1 (R1-8) item (1).


## 2. Bootstrap execution — every environment, every CI job that runs Alembic

Alembic NEVER creates extensions or roles, and nothing may run `alembic upgrade head` against a database where `scripts/db/0000_bootstrap.sql` has not run. Three named owners, no fourth path:

1. LOCAL — the compose mount above on a fresh volume; on an existing volume the dev runs `make bootstrap-db`. Add this root Makefile target verbatim:
```make
.PHONY: bootstrap-db
bootstrap-db:
	psql "postgresql://postgres:postgres@localhost:5432/marketplace_os" \
	  -v ON_ERROR_STOP=1 -f scripts/db/0000_bootstrap.sql
```
and `make dev` calls `bootstrap-db` between `docker compose up -d` and `alembic upgrade head`.

2. CI — every job with a postgres service gets this step immediately before its first Alembic invocation (change only the database name: `mos_test` for api-tests, `mos_iso` for tenant-isolation):
```yaml
      - name: bootstrap extensions and roles
        run: |
          psql "postgresql://postgres:postgres@localhost:5432/mos_test" \
            -v ON_ERROR_STOP=1 -f scripts/db/0000_bootstrap.sql
```
`psql` is preinstalled on `ubuntu-latest`; no extra install step. GitHub service containers cannot mount initdb.d and MUST NOT be asked to.

3. STAGING / PROD — owner: Tech Lead, Day 3 (Wed 23 Sep 2026). Run once per Cloud SQL instance through the Cloud SQL Auth Proxy as the `postgres` user: `psql "$BOOTSTRAP_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/db/0000_bootstrap.sql`. `BOOTSTRAP_DATABASE_URL` is a Secret Manager secret readable only by the Tech Lead and the migrator service account; it is NOT an env-register row and never reaches a container.

The `bootstrap` required check (see the required-checks ruling) is what keeps this true.

> **Supersedes:** The migration_ledger 0000 row's 'run once per environment' with no named step, job or owner. Resolves kick_verify unresolved #3 (R0-2 / R1-1) item (1).


## 3. Alembic driver dependency and THE authoritative Python manifest

`apps/api/pyproject.toml` is THE Python manifest for the API, the workers, Alembic and `scripts/`. The manifest in kick_drafts sec2 code_block 11 is DELETED — it is wrong in four further ways (`requires-python = ">=3.12"`, `python-jose`, `passlib[bcrypt]`, `cloud-sql-python-connector`), all of which contradict already-ruled canon rows.

Add exactly this line to `apps/api/pyproject.toml` `[project].dependencies`, immediately after the `asyncpg` line:
```toml
  "psycopg[binary]>=3.2",          # SYNC driver: Alembic + psql-less scripts only
```
The manifest's frozen facts: `requires-python = "==3.11.*"`; `asyncpg>=0.29` is the only driver the app uses; `pyjwt[crypto]>=2.9` (python-jose forbidden); `pwdlib[argon2]` (passlib forbidden); no `cloud-sql-python-connector` (R1-12 deferred to an ADR); no `numpy`. Regenerate `uv.lock` in the same PR — `uv sync --frozen` in CI and in the Dockerfile fails on a stale lock.

> **Supersedes:** The apps/api dependency manifest in kick_drafts sec0 code_block 4 (asyncpg only, no psycopg) and the rival manifest in sec2 code_block 11. Resolves kick_verify unresolved #1 (R1-8) item (3).


## 4. 0002 `rules` table — the full DDL

The migration_ledger 0002_listing_pipeline `rules` entry is replaced by this column list, which is what `mkos_schemas.rules.Rule`, `packages/schemas/fixtures/rules.seed.json` and the seed command already write:

```sql
CREATE TABLE rules (
  id          UUID PRIMARY KEY,
  tenant_id   UUID NULL REFERENCES tenants(id),        -- NULL = platform-global
  marketplace TEXT NOT NULL CHECK (marketplace IN ('amazon','flipkart')),
  category    TEXT NULL,                               -- NULL = every category
  field       TEXT NOT NULL,                           -- paths.py path, or '*'
  severity    TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('error','warning')),
  version     INTEGER NOT NULL DEFAULT 1,
  enabled     BOOLEAN NOT NULL DEFAULT true,
  rule_def    JSONB NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX rules_scope_key ON rules (
  marketplace,
  COALESCE(category, ''),
  field,
  version,
  COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)
);
CREATE INDEX rules_lookup_idx ON rules (marketplace, category) WHERE enabled;
```
The plain `UNIQUE (marketplace, category, field, version)` in the canon row is DELETED: with a nullable `category` and a nullable `tenant_id` a plain UNIQUE does not dedupe, so two identical platform rules would both insert and the ruleset would double-apply.

> **Supersedes:** The migration_ledger 0002 text 'rules (platform-global, nullable tenant_id, UNIQUE (marketplace, category, field, version))'. Resolves kick_verify unresolved #3 (R0-2 / R1-1) item (2).


## 5. Seed — one entrypoint, one fixture set

ONE invocation: `python -m app.cli seed --profile {demo|isolation}`. `--profile` is REQUIRED and has no default.

Mechanics: the seed body moves to `apps/api/app/cli.py` (argparse sub-command `seed`, flags `--profile` and `--reset`). `scripts/seed.py` is DELETED. The fixture path inside `app/cli.py` is `Path(__file__).resolve().parents[3] / "packages/schemas/fixtures/rules.seed.json"`. Callers:
- root Makefile: `seed:\n\tuv run python -m app.cli seed --profile demo`
- `apps/api/docker-entrypoint.sh`, seed role, replaced verbatim: `exec python -m app.cli seed --profile "${SEED_PROFILE:-demo}"`
- the import-linter allowlist for `platform_scope` becomes `app.admin.*`, `app.cli`, `scripts.*`.

FROZEN FIXTURE SKUs (the shipped set wins; `SKU-001` does not exist and must not appear in any test, row or document):
- tenants: `acme` ("Acme Apparel"), `bharat` ("Bharat Home")
- shared by BOTH tenants, in both profiles — this collision IS the isolation fixture: `KURTA-001` (Cotton Kurta Set, `ethnic_wear_kurta_sets`), `KURTA-002` (Silk Kurta Set, `ethnic_wear_kurta_sets`), `DECOR-001` (Brass Table Lamp, `home_decor_lamps`)
- `--profile demo` adds one tenant-specific product each: `ACME-900` (Linen Nehru Jacket), `BH-900` (Jute Floor Rug) → 4 products per tenant
- `--profile isolation` = the three shared SKUs only → 3 products per tenant
- users: one per role per tenant, roles `owner|operator|reviewer|admin`, `{role}@{slug}.test`, password `devpassword123`
- profile `costs` is DELETED from the canon — no fixture, no code, no test behind it.

The products-uniqueness canon row's proof sentence becomes: "both tenants hold KURTA-001, KURTA-002 and DECOR-001".

> **Supersedes:** The canon seed row (`--profile demo|isolation|costs`, 'overlapping SKU-001', '3 products each'), scripts/seed.py's `make seed -> uv run python scripts/seed.py` docstring and its `default="isolation"`, and docker-entrypoint.sh's `exec python /src/scripts/seed.py`. Resolves kick_verify unresolved #1 (R1-8) item (2) and internal conflict #9 (two seed contracts).


## 6. Tenant-guard bypass — the one mechanism

`session.info` is the mechanism. SQLAlchemy has no session-level `execution_options` setter, so the canon's wording was not implementable; it is rewritten to the shipped `app/db/session.py` behaviour, and the guard change is part of the ruling, not an undocumented extra.

1. `apps/api/app/db/guard.py` declares `SCOPE_OK = "tenant_scope_checked"` and the `do_orm_execute` body opens with exactly:
```python
    if state.execution_options.get(SCOPE_OK) or state.session.info.get(SCOPE_OK):
        return
```
2. `platform_scope(*, readonly: bool = True)` in `apps/api/app/db/session.py` sets `session.info[SCOPE_OK] = True` and issues `SET TRANSACTION READ ONLY` when `readonly` is true. It is the only place that writes `SCOPE_OK`.
3. `readonly=False` carve-out: permitted from `apps/api/app/cli.py` and `scripts/**` ONLY (seeding, and DLQ requeue on staging). Every other caller uses the default.
4. The CI grep in the `lint-imports` job is: `grep -rn "tenant_scope_checked" --include='*.py' apps packages scripts` must return exactly two files — `apps/api/app/db/guard.py` and `apps/api/app/db/session.py`. Any third hit fails the job.
5. import-linter forbidden contract: `platform_scope` importable only from `app.admin.*`, `app.cli`, `scripts.*`.

> **Supersedes:** The canon row 'a session stamped execution_options(tenant_scope_checked=True, readonly=True)' and its unqualified 'CI greps for any other occurrence'. Resolves internal conflict #8.


## 7. `bootstrap` required-check job

`bootstrap` is a real GitHub job with the check name exactly `bootstrap`, in `.github/workflows/ci-api.yml`, no `paths:` filter. It proves the documented trace from a clean clone on every PR to main (`localhost:8000` is the default API port; a second worktree uses its own):
```yaml
  bootstrap:                      # required check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/py-setup
      - run: cp .env.example .env
      - run: docker compose up -d --wait        # fails if any service is unhealthy
      - run: make bootstrap-db
      - run: uv run alembic -c apps/api/alembic.ini upgrade head
      - run: uv run python -m app.cli seed --profile demo
      - run: uv run uvicorn app.main:app --port 8000 --app-dir apps/api &
      - run: |
          for i in $(seq 1 30); do curl -sf localhost:8000/healthz && break; sleep 2; done
          curl -sf localhost:8000/healthz && curl -sf localhost:8000/readyz
      - run: docker build -f apps/api/Dockerfile -t mkos-api:ci .
      - run: docker run --rm -e APP_ROLE=shell mkos-api:ci -c "import app, mkos_schemas, mkos_pipeline"
```
Its env block is the four-variable block below. `docker compose up -d --wait` replaces 'docker compose ps must show 3 healthy' as the mechanical assertion.

> **Supersedes:** The canon R1-8 resolution's unnamed 'A `bootstrap` CI job boots the stack from a clean clone', which was promised but appeared in no required-check list. Resolves kick_verify unresolved #1 (R1-8) closing clause.


## 8. Settings fields — the ten missing, plus extra="forbid"

`model_config` becomes:
```python
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="forbid", case_sensitive=False)
```
The four hard-required fields stay four (`app_env`, `database_url`, `redis_url`, `jwt_secret`) so the CI env block stays four. Add these ten fields verbatim:
```python
    # ---- app / admin ----
    admin_session_secret: str = Field(min_length=32,
        default="dev_only_admin_session_secret_at_least_32_chars")
    admin_base_url: str = "/internal/admin"
    admin_local_email: str = "dev@local"
    oauth_state_ttl_seconds: int = 600
    password_hash_scheme: Literal["argon2id"] = "argon2id"
    # ---- marketplaces ----
    amazon_app_id: str | None = None
    amazon_lwa_token_url: str = "https://api.amazon.com/auth/o2/token"
    flipkart_token_url: str = "https://api.flipkart.net/oauth-service/oauth/token"
    flipkart_api_base: str = "https://api.flipkart.net/sellers"
    # ---- observability ----
    alert_slack_webhook: str | None = None
```
Because `extra="forbid"` also rejects the FE keys that live in the single root `.env`, declare these three as well — the API never reads them, they exist so the shared file validates:
```python
    # FE-only keys; apps/web reads them from apps/web/.env.local. Declared here
    # solely so extra="forbid" accepts the single root .env.
    next_public_api_base_url: str = "http://localhost:8000/api/v1"
    nextauth_url: str = "http://localhost:3000"
    nextauth_secret: str = "dev_only_nextauth_secret_at_least_32_chars"
```
And `_deployed_requirements` gains one line, so the local admin secret can never be deployed:
```python
        if self.admin_session_secret == "dev_only_admin_session_secret_at_least_32_chars":
            missing.append("ADMIN_SESSION_SECRET (the local default may not be deployed)")
```
The docstring line 'Nothing else in the codebase reads os.environ' is now true and is enforced by the ruff `T20`/custom check on `os.environ` outside `app/core/config.py`.

> **Supersedes:** `extra="ignore"` in apps/api/app/core/config.py and the ten register rows that had no field. The ADMIN_SESSION_SECRET register row's `required` column changes to 'required in staging/production; defaulted locally'. The AMAZON_APP_ID row's `required` column changes to 'required before first staging publish; blank until the SP-API application is approved'. Resolves kick_verify unresolved #2 (R0-3 / R1-14) first half.


## 9. config.py defaults that disagree with the register

The register wins on all four. Four exact edits in `apps/api/app/core/config.py`:
1. `arq_job_timeout_seconds: int = 300` → `arq_job_timeout_seconds: int = 1800` (ceiling only; per-function timeouts stay generation 300s / publish 600s / imports 1800s, set on the ARQ function definitions, not on this knob).
2. `otel_service_name: str = "api"` → `otel_service_name: str = "marketplace-os-api"` (the worker deployments set `marketplace-os-worker`; bare `api`/`worker` are forbidden).
3. `kms_local_dev_key: str = "bG9jYWwtZGV2LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="` → `kms_local_dev_key: str = "bG9jYWxfZGV2X29ubHlfMzJfYnl0ZV9rZXlfMDAwMDA="` (decodes to `local_dev_only_32_byte_key_00000`, 32 bytes — byte-identical to the register example, so a laptop and CI unseal each other's LocalKmsStub blobs).
4. `port: int = 8080` → `port: int = 8000`. The Dockerfile KEEPS `ENV PORT=8080` and `EXPOSE 8080` and Cloud Run injects `$PORT`: the env var always wins over the default, so the container is unaffected and every canonical local URL (JWT_ISSUER, NEXT_PUBLIC_API_BASE_URL, both OAuth redirect URIs, the bootstrap curl) is consistent at `localhost:8000`.

> **Supersedes:** The four defaults shipped in apps/api/app/core/config.py. Resolves internal conflict #5.


## 10. Four new env-register rows

Add four rows to the env register so config.py declares nothing the register does not carry:
| name | example | where | required | group | notes |
|---|---|---|---|---|---|
| `PORT` | `8000` | local .env | required | App | 8000 locally. Cloud Run injects `$PORT` (image default 8080); the worker health server binds `settings.port`, which is why this is a register row and not a hard-coded constant. |
| `DATABASE_POOL_TIMEOUT` | `10` | local .env | required | Database | Seconds to wait for a pooled connection before raising. |
| `DATABASE_POOL_RECYCLE` | `1800` | local .env | required | Database | Cloud SQL drops idle connections; recycle below that window. |
| `DB_STATEMENT_TIMEOUT_MS` | `15000` | local .env | required | Database | Passed as `statement_timeout` in `connect_args.server_settings`. |
The register is then 83 rows. Every row appears in the single root `./.env.example` in register order, and the Secret Manager seeding script is regenerated from the register in the same PR.

> **Supersedes:** The preamble's '80 rows' and the register's actual 79. Resolves kick_verify unresolved #2 (R0-3 / R1-14) second half and the row-count half of internal conflict #10.


## 11. CI env block — every job that imports the app

`ENV:` is deleted from every workflow file in the pack; it is not a Settings field and with `extra="forbid"` it is now an error, not a no-op. Every job that imports `app.*` (api-lint, api-typecheck, api-tests, tenant-isolation, contracts, schemas-drift, bootstrap) carries EXACTLY this block, changing only the database name:
```yaml
    env:
      APP_ENV: local
      DATABASE_URL: postgresql+asyncpg://postgres:postgres@localhost:5432/mos_test
      REDIS_URL: redis://localhost:6379/0
      JWT_SECRET: ci_only_not_a_real_secret_at_least_32_chars
```
Notes that are part of the ruling: `JWT_SECRET` must be ≥32 characters — the pack's `ci-only-not-a-real-secret` is 25 and fails `Field(min_length=32)`; the string above is 43. `tenant-isolation` uses `mos_iso` AND gains the `redis:7-alpine` service plus the `REDIS_URL` line it currently omits. `contracts` and `schemas-drift` need no reachable database: `get_engine()` is lazy and `Settings` only pattern-checks the URL strings.

> **Supersedes:** `ENV: test` and the missing `APP_ENV` in kick_drafts sec0 code_block 8 (api-tests), the missing `REDIS_URL` in tenant-isolation, and the env-less contracts workflow in sec5 code_block 16. Resolves kick_verify unresolved #2 (R0-3 / R1-14) CI half.


## 12. Required checks — nine become twelve

Branch protection on `main` requires TWELVE checks, names exactly: `api-lint`, `api-typecheck`, `api-tests`, `tenant-isolation`, `contracts`, `schemas-drift`, `web-lint`, `web-typecheck`, `commit-lint`, `bootstrap`, `lint-imports`, `gitleaks`. No `paths:` filter on any workflow carrying any of them.

The three new ones:
- `lint-imports` — `uv run lint-imports --config .importlinter`. Enforces: `packages.pipeline` may not import `apps.api`; `platform_scope` importable only from `app.admin.*`, `app.cli`, `scripts.*`; the credentials modules may not be imported by `packages.pipeline`. Same job also runs the `tenant_scope_checked` grep from the tenant-guard ruling.
- `gitleaks` — `gitleaks detect --redact --no-banner --exit-code 1` over the PR range, with `.gitleaks.toml` at the repo root. This is the mechanical half of the Day-41 no-secrets-in-git guarantee.
- `bootstrap` — as defined above.
Both `mypy` (inside `api-typecheck`, `--strict` over `apps/api` + `packages/schemas`) and `eslint` (inside `web-lint`, flat config, `--max-warnings=0`) remain required, unchanged.

> **Supersedes:** The nine-item required_ci_checks list. Resolves internal conflict #11 second half.


## 13. packages/schemas Makefile — the openapi target's env

`make -C packages/schemas all` shells into `apps/api` and imports `app.main`, which now hard-exits without the four variables. The `openapi` target is rewritten so it works on a laptop and in CI with no database and no Redis reachable:
```make
openapi:
	cd ../../apps/api && \
	  APP_ENV=local \
	  DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/marketplace_os \
	  REDIS_URL=redis://localhost:6379/0 \
	  JWT_SECRET=local_only_schema_export_secret_32_chars \
	  uv run python -c "import json; from app.main import app; \
	    print(json.dumps(app.openapi(), indent=2, sort_keys=True))" \
	  > ../../packages/schemas/jsonschema/openapi.json
	npx --yes openapi-typescript@7 jsonschema/openapi.json -o ts/src/api.d.ts
```
The openapi path is `packages/schemas/jsonschema/openapi.json` and nothing else; the legacy `packages/schemas/openapi.json` and `packages/schemas/generated/index.ts` are deleted. The drift gate stays `git diff --exit-code -- packages/schemas/jsonschema packages/schemas/ts`.

> **Supersedes:** The `openapi:` recipe in kick_drafts sec5 code_block 15 and the env-less contracts workflow. Resolves internal conflict #11 first half.


## 14. ADMIN_ENABLED kill-switch — the exact CI test fixture

The production guard is NOT relaxed. The test constructs `Settings` with a complete production fixture, so the guard and the 404 assertion coexist. File: `apps/api/tests/test_admin_killswitch.py`, no marker, runs inside `api-tests`:
```python
PROD_ENV = {
    "APP_ENV": "production",
    "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:5432/x",
    "REDIS_URL": "redis://127.0.0.1:6379/0",
    "JWT_SECRET": "x" * 32,
    "ADMIN_ENABLED": "false",
    "ADMIN_SESSION_SECRET": "y" * 32,
    "GCP_PROJECT_ID": "aimos-prod",
    "KMS_KEY_URI": "projects/aimos-prod/locations/asia-south1/keyRings/"
                   "marketplace-os/cryptoKeys/oauth-token-key",
    "OPENAI_API_KEY": "sk-test-not-a-real-key",
    "SENTRY_DSN": "https://public@o0.ingest.sentry.io/0",
    "STORAGE_BACKEND": "gcs",
}
```
The test monkeypatches os.environ to `PROD_ENV`, calls `get_settings.cache_clear()`, builds the app factory, and asserts `client.get("/internal/admin").status_code == 404`. A second case sets `ADMIN_ENABLED="true"` and asserts `SystemExit` — the guard itself is the kill switch and both halves are covered.
The SENTRY_DSN register row's `required` column changes from `optional` to `optional when APP_ENV=local, REQUIRED when APP_ENV != local`, matching `_deployed_requirements`.

> **Supersedes:** The ADMIN_ENABLED canon row's unqualified 'a CI test asserts /internal/admin returns 404 when APP_ENV=production', and the SENTRY_DSN register row's `optional`. Resolves internal conflict #6.


## 15. apps/api/Dockerfile — the empty venv

`uv sync` installs into the project environment and ignores a mismatched `VIRTUAL_ENV`, so the runtime stage currently copies an empty `/opt/venv`. Two edits:
1. In the builder stage, immediately after `FROM base AS builder`, add:
```dockerfile
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
```
`VIRTUAL_ENV=/opt/venv` and `PATH="/opt/venv/bin:$PATH"` stay on `base` for the runtime stage. `RUN uv venv "$VIRTUAL_ENV"` stays.
2. In `apps/api/docker-entrypoint.sh` the shell role currently swallows its arguments. Replace it with:
```sh
  shell)
    exec python "$@"
    ;;
```
so `docker run --rm -e APP_ROLE=shell mkos-api:ci -c "import app, mkos_schemas, mkos_pipeline"` actually executes the import. That `docker build` + `docker run` pair is the last two steps of the `bootstrap` job and is what stops an empty-venv image ever reaching Cloud Run.

> **Supersedes:** The builder stage of apps/api/Dockerfile and the `shell)` branch of apps/api/docker-entrypoint.sh. Resolves internal conflict #12.


## 16. RuleSet / Rule — one definition

`packages/schemas/src/mkos_schemas/rules.py` is the canonical and ONLY definition. The canon's inline field list — `RuleSet = (marketplace, category, version, rules: list[Rule])` with `Rule = field_path, kind, params, severity, message, code` — is DELETED, because rules.py is the shape with a table, a fixture file and a seed behind it.

Frozen names, no synonyms anywhere in code, DDL, fixtures or prose:
- `Rule` = `id, tenant_id, marketplace, category, field, severity, version, enabled, rule_def`
- `RuleSet` = `marketplace, category, rules, rules_version, loaded_at`
- `RuleDef` = discriminated union on `type`, members `max_length | min_length | banned_terms | required_attribute | max_items`, each carrying `message`, optional `suggestion` and `error_code`
- forbidden spellings: `field_path`, `kind`, `params`, `version` (on RuleSet — it is `rules_version`)

The ai_seam register's fifth rule is rewritten to: "`RuleSet` and `Rule` are declared in `packages/schemas/src/mkos_schemas/rules.py`. No field list is repeated here or anywhere else; that file is the contract."

> **Supersedes:** The ai_seam register row listing RuleSet/Rule fields inline. Resolves internal conflict #1.


## 17. rules.py RuleSet docstring

`load_rules` no longer exists on `PipelineDeps` — context is pushed. Replace the `RuleSet` docstring in `packages/schemas/src/mkos_schemas/rules.py` with exactly:
```python
    """Carried on GenerationContext.marketplace_rules; the backend resolves it
    before enqueue. PipelineDeps has NO load_rules port — the AI package fetches
    nothing. Resolution order: a tenant rule shadows the platform rule with the
    same (marketplace, category, field), and a category rule shadows the
    category=NULL rule."""
```
Same edit discipline for any other artifact docstring naming a deleted port: `load_rules`, `load_category_template`, `validate_schema` and `record_agent_run` may not appear as `PipelineDeps` members in any file.

> **Supersedes:** The shipped rules.py docstring 'What PipelineDeps.load_rules() returns'. Resolves internal conflict #2.


## 18. PipelineStage and JobState — enums.py and state.py

Before the `schemas-v1.0.0` tag, `packages/schemas/src/mkos_schemas/enums.py` carries exactly these two enums:
```python
class PipelineStage(StrEnum):
    """TRANSIENT. Surfaced as JobEnvelope.stage, persisted only in job_runs.node.
    NEVER written to listings.status."""
    LOAD_CONTEXT = "load_context"
    RETRIEVE_BRAND = "retrieve_brand"
    CONTENT_AGENT = "content_agent"
    KEYWORD_AGENT = "keyword_agent"
    ATTRIBUTE_AGENT = "attribute_agent"
    IMAGE_AGENT = "image_agent"
    MERGE_OUTPUTS = "merge_outputs"
    VALIDATE_SCHEMA = "validate_schema"
    VALIDATE_RULES = "validate_rules"
    VALIDATE_VISION = "validate_vision"
    CORRECT = "correct"
    QUALITY_CHECK = "quality_check"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"
```
Deleted members, never to reappear: `LOADING_CONTEXT`, `RETRIEVING_BRAND`, `GENERATING`, `MERGING`, `VALIDATING_SCHEMA`, `VALIDATING_RULES`, `VALIDATING_VISION`, `CORRECTING`, `SCORING`, `AWAITING_APPROVAL`, and `JobState.TIMEOUT`.

`packages/pipeline/src/mkos_pipeline/state.py`, `initial_state()`, one edit:
`stage=PipelineStage.LOADING_CONTEXT` → `stage=PipelineStage.LOAD_CONTEXT`.

> **Supersedes:** The 10-member PipelineStage and 6-member JobState in kick_drafts sec5 code_block 2, and state.py's `PipelineStage.LOADING_CONTEXT`. Resolves internal conflict #3.


## 19. ErrorSource for import-row failures, and validation_runs.source

`rules` is not and never becomes an `ErrorSource` member. Two bindings:
- a CSV row that fails `ProductImportRow` validation → `source=ErrorSource.SCHEMA` (`"schema"`)
- a CSV row that fails a rules/catalog lookup, i.e. `CATEGORY_NOT_MAPPED` → `source=ErrorSource.RULES_ENGINE` (`"rules_engine"`)
The CSV intro sentence becomes: "a failed import row writes `error_report_json` carrying `source="schema"` (row shape) or `source="rules_engine"` (category/rule lookup)". The literal `"rules"` is forbidden in code, fixtures and prose.

`validation_runs.source` CHECK carries ALL SEVEN ErrorSource values, not five:
```sql
  source TEXT NOT NULL CHECK (source IN
    ('schema','rules_engine','vision_model','ai_model','amazon_adapter',
     'flipkart_adapter','system'))
```
`ai_model` and `system` are restored — a generation-model failure and a handler failure both need a validation_runs row, and dropping them reproduces exactly the `source="api"` 500 the canon was written to end.

> **Supersedes:** The CSV intro's `source="schema" or "rules"` and the validation_runs canon row's five-value list. Resolves internal conflict #4.


## 20. codes.py — the five new error codes

Add these to `ErrorCode` in `packages/schemas/src/mkos_schemas/codes.py` before the `schemas-v1.0.0` tag, in these sections:
```python
    # --- source=schema  (bulk import: header is file-level, SKU is row-level) --
    HEADER_MISSING_COLUMN = "HEADER_MISSING_COLUMN"
    HEADER_UNKNOWN_COLUMN = "HEADER_UNKNOWN_COLUMN"
    HEADER_DUPLICATE_COLUMN = "HEADER_DUPLICATE_COLUMN"
    DUPLICATE_SKU_IN_FILE = "DUPLICATE_SKU_IN_FILE"
    # --- source=system -------------------------------------------------------
    JOB_TIMEOUT = "JOB_TIMEOUT"
```
`TIMEOUT` stays and keeps its meaning (a single model call under `source=ai_model`); `JOB_TIMEOUT` is the job-level one recorded on a `failed` job_runs row under `source=system`. Both exist; they are not synonyms.

No bare strings. `check_header()` in `apps/api/app/services/imports/row_model.py` changes signature from `-> list[str]` to:
```python
def check_header(columns: list[str]) -> list[tuple[ErrorCode, str]]:
```
returning `(ErrorCode.HEADER_MISSING_COLUMN, column_name)` tuples; the import route maps them into `ErrorReport(status="failed", error_code=ErrorCode.IMPORT_FAILED, errors=[...])` with `field="file.header"`. Add `"file.header"` and `"file.row"` to the `paths.py` vocabulary so `ErrorItem._path` accepts them.

Standing rule: a new error code is a PR against `packages/schemas/src/mkos_schemas/codes.py` reviewed by the Tech Lead. No module may emit an f-string or literal where an `ErrorCode` belongs; ruff catches it via the existing ban on string literals in `error_code=` positions.

> **Supersedes:** The canon's assertion that codes.py is 'the single error-code registry' while five mandated codes were absent from it, and row_model.py's f-string error list. Resolves kick_verify unresolved #5 (R1-2).


## 21. AI seam — where each named type lives, and the canonical module layout

`packages/schemas/src/mkos_schemas/pipeline.py` declares the `PipelineDeps` Protocol and the `run_listing_pipeline` signature ONLY, and RE-EXPORTS every other type the seam names. Nothing is redeclared; there is never a second `GenerationContext`.
```python
from .agent import AgentRunRecord
from .jobs import (AgentBlockStatus, BrandChunkDTO, CategoryTemplate,
                   GenerationContext, GenerationResult, ModelCredentials)
from .rules import Rule, RuleSet

__all__ = ["PipelineDeps", "run_listing_pipeline", "GenerationContext",
           "GenerationResult", "AgentBlockStatus", "AgentRunRecord",
           "BrandChunkDTO", "CategoryTemplate", "ModelCredentials",
           "Rule", "RuleSet"]
```
OWNERSHIP, one module each:
- `jobs.py` — GenerateListingRequest, JobAccepted, JobEnvelope, GenerationContext, GenerationResult, AgentBlockStatus, BrandChunkDTO, CategoryTemplate, ModelCredentials
- `rules.py` — Rule, RuleDef and its five members, RuleSet
- `agent.py` — TokenUsage, AgentRunRecord
- `pipeline.py` — PipelineDeps, run_listing_pipeline, and the re-exports above
`packages/pipeline/src/mkos_pipeline/state.py` keeps importing from `mkos_schemas.jobs` — that is the owning module and is correct.

The canonical layout is SEVENTEEN files:
`packages/schemas/src/mkos_schemas/{__init__,base,enums,codes,paths,listing,errors,images,agent,validation,jobs,publish,imports,auth,rules,pipeline,export}.py`
`export.py` is what `make -C packages/schemas json` invokes (`python -m mkos_schemas.export --out jsonschema/`) and without it the schemas-drift gate has no generator; `rules.py` is the RuleSet/Rule home.

> **Supersedes:** The naming register's 15-file layout, and the ai_seam `module_path` claim that pipeline.py holds 'every type it names'. Resolves kick_verify unresolved #4 (R0-1 / R1-3).


## 22. apps/web/eslint.config.mjs — ignore path and restricted imports

`apps/web/src/lib/api-types.ts` is deleted; generated TS is `@mkos/schemas`. Three exact edits to `apps/web/eslint.config.mjs`:
1. Delete the ignore entry `"src/lib/api-types.ts", // generated by \`make gen:types\`; drift-checked, never linted`. The generated files now live in `packages/schemas/ts` and are outside this config's root, so no ignore entry is needed.
2. `no-restricted-syntax` message becomes: `"Import it from @mkos/schemas (generated from packages/schemas/jsonschema/openapi.json)."`
3. Add a pattern to `no-restricted-imports` so the deleted path can never come back:
```js
      "no-restricted-imports": ["error", {
        paths: [{ name: "axios", message: "Use the openapi-fetch client in src/lib/api.ts." }],
        patterns: [
          { group: ["../../*"], message: "Use the @/ alias." },
          { group: ["**/lib/api-types", "**/lib/api-types.*", "@/lib/api-types"],
            message: "Deleted. Import generated types from @mkos/schemas — it is the only path the schemas-drift gate guards." },
        ],
      }],
```

> **Supersedes:** The `src/lib/api-types.ts` ignore entry and the 'Import it from src/lib/api-types.ts' message in the shipped apps/web/eslint.config.mjs. Resolves internal conflict #7.


## 23. apps/web/package.json — generated-types scripts

Two edits, so the FE has no second route to a generated type and no second linter:
1. DELETE the `gen:types` script entirely (`"gen:types": "openapi-typescript ../../packages/schemas/openapi.json -o ../../packages/schemas/generated/index.ts"`). Type generation is `make -C packages/schemas all` at the repo root and nowhere else.
2. `"lint": "next lint --max-warnings=0"` → `"lint": "eslint . --max-warnings=0"`. `next lint` does not read `eslint.config.mjs` flat config, so the local command and the `web-lint` gate were running different rule sets.
Add `"@mkos/schemas": "workspace:*"` to `dependencies` in the same PR.

> **Supersedes:** The apps/web package.json in kick_drafts sec0 code_block 12. Cosmetic half of internal conflict #7.


## 24. Cloud Run connection arithmetic for two worker services

Three services, not two. The budget recorded in ADR-0003 (Cloud Run) is restated as:

`(api max-instances 3 + q:default max-instances 2 + q:publish max-instances 2) x (pool_size 5 + max_overflow 5) = 7 x 10 = 70`

Checked against the instance: staging and production Cloud SQL run `db-custom-2-7680` with the `max_connections` flag set EXPLICITLY to `200`. Ledger: 70 application + 5 pre-deploy migration job + 10 Cloud SQL Auth Proxy / laptop sessions + 3 reserved superuser = 88 of 200. It fits with headroom, so nothing is lowered: pool stays 5/5, api `--max-instances=3`, each worker `--min-instances=1 --max-instances=2 --no-cpu-throttling`.

The `max_connections=200` flag is set on the instance by the Tech Lead in the same Day-3 step that runs `scripts/db/0000_bootstrap.sql`, and any change to pool size, max-instances or the number of worker services requires this arithmetic to be recomputed in the ADR in the same PR.

> **Supersedes:** The R1-13 resolution's '(api max-instances 3 + worker max-instances 2) x 10 = 50', which counted one worker service while the same ruling mandates two. Resolves internal conflict #9 (Cloud Run arithmetic).


## 25. Migration-ledger counts

Three counts corrected to match their own enumerations, in the document whose purpose is to be counted against:
- 0002_listing_pipeline: 'The ten remaining domain tables' → **'The eleven remaining domain tables'** (listings, listing_versions, validation_runs, agent_runs, audit_log, images, performance, marketplace_accounts, rules, brand_chunks, brand_documents = 11).
- 0003_ops_tables: 'The five ops tables' → **'The six ops tables'** (job_runs, imports, import_rows, idempotency_keys, refresh_tokens, category_templates = 6).
- Preamble / R0-4 resolution: 'one consolidated env register — 80 rows' → **'83 rows'** (79 shipped + PORT + DATABASE_POOL_TIMEOUT + DATABASE_POOL_RECYCLE + DB_STATEMENT_TIMEOUT_MS).
Standing rule: a count in THE CANON is generated from the enumeration it precedes, never typed by hand; a PR that adds a table or a row updates the count in the same diff.

> **Supersedes:** The 'ten'/'five'/'80' figures in migration_ledger 0002, migration_ledger 0003 and the preamble. Resolves internal conflict #10.


## 26. Redirect URIs — the count is six, and the placeholder token

SIX frozen strings, not nine. The canon's 'nine exact redirect-URI strings (3 environments x Amazon/Flipkart, plus local)' double-counts local; local IS one of the three environments. The filer copies this one block and nothing else:

```
Amazon   local:   http://localhost:8000/api/v1/marketplaces/amazon/callback
Amazon   staging: https://api-staging.<APEX>/api/v1/marketplaces/amazon/callback
Amazon   prod:    https://api.<APEX>/api/v1/marketplaces/amazon/callback
Flipkart local:   http://localhost:8000/api/v1/marketplaces/flipkart/callback
Flipkart staging: https://api-staging.<APEX>/api/v1/marketplaces/flipkart/callback
Flipkart prod:    https://api.<APEX>/api/v1/marketplaces/flipkart/callback
```

FROZEN NOW, independent of the domain: the scheme-relative remainder `/api/v1/marketplaces/{amazon|flipkart}/callback`, the `api-staging.` and `api.` host prefixes, and the two local strings, which are complete and are the `AMAZON_OAUTH_REDIRECT_URI` / `FLIPKART_OAUTH_REDIRECT_URI` defaults in config.py today.

The placeholder token is the literal `<APEX>`, everywhere, replacing every `<domain>` in the naming register, the env register (ALLOWED_ORIGINS, JWT_ISSUER, KMS_KEY_URI notes) and all prose — one greppable token so the substitution is a single `sed` when the answer lands. `grep -rn '<APEX>' .env.example docs/ packages/ apps/` is a step in the `bootstrap` job that WARNS today and becomes a hard failure the moment a staging deploy workflow is added. Until then `AMAZON_OAUTH_REDIRECT_URI` and `FLIPKART_OAUTH_REDIRECT_URI` carry only the local strings, and no staging/prod value is seeded into Secret Manager.

> **Supersedes:** The R0-1/R1-6 resolution's 'nine exact redirect-URI strings' and every `<domain>` placeholder in the naming and env registers. Resolves the countable half of kick_verify unresolved #6; the domain itself is in still_open below.


---

## Still open — needs a human answer

| Open item | Who decides | By when |
|---|---|---|
| The apex domain (`<APEX>`) for the four non-local redirect URIs, plus ALLOWED_ORIGINS and JWT_ISSUER for staging and production. The Canon must not invent one — a wrong guess costs the full Amazon SP-API re-review queue (2-4 weeks), which is longer than the remaining 43 days of the sprint. INTERIM RULE, in force from today and independent of the answer: (1) the path segment `/api/v1/marketplaces/{marketplace}/callback` and the host prefixes `api-staging.` / `api.` are FROZEN by the ruling above, so only the apex label is unknown and only a host substitution remains; (2) when the client names two or more candidate apexes, BOTH marketplace applications are filed with the staging AND prod forms of EVERY candidate listed together in the same submission — Amazon SP-API and the Flipkart seller API both accept multiple redirect URIs per application, so a later pick between pre-registered candidates needs no resubmission and no re-review; (3) if no candidate exists by the deadline, the Tech Lead nominates a fallback apex the team can register and control the same day, files against it, and the client's later preferred domain is added as an amendment rather than delaying the queue start; (4) until the substitution happens, `AMAZON_OAUTH_REDIRECT_URI` / `FLIPKART_OAUTH_REDIRECT_URI` hold only the local strings, no staging/prod redirect value is seeded into Secret Manager, and the `<APEX>` grep in the `bootstrap` job blocks any staging deploy workflow from merging. | Client (domain owner), relayed by the Tech Lead. One answer needed: the apex the production and staging APIs will live on, or the list of candidates to pre-register. | Wed 23 Sep 2026, 18:00 IST (Day 3) — the Amazon application must be filed Day 3 at the latest for a 2-4 week approval to clear before the Day-45 launch on Thu 5 Nov 2026. No answer by then triggers interim rule (3) automatically; the Tech Lead does not wait further. |
| Which marketplace developer accounts the six redirect URIs are filed under: the client's EXISTING Amazon SP-API developer account and Flipkart seller account, or new ones registered by us. This determines who owns `AMAZON_APP_ID`, `AMAZON_CLIENT_ID` / `AMAZON_CLIENT_SECRET` and the Flipkart credentials, and whether an existing approved application can simply have redirect URIs added (days) or a fresh application must be submitted (2-4 weeks). It cannot be ruled technically — it is an account-ownership and commercial question. | Client (seller-account owner) with the Tech Lead. If the client already holds an approved SP-API application, we need the developer-account ID and someone with console access to add the URIs. | Wed 23 Sep 2026 (Day 3), same conversation as the apex domain — both answers feed the same filing. |
