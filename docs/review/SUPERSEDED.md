# SUPERSEDED — draft values that are WRONG, and pending scaffold edits

Three sources contain values the Canon later overruled:
- the pre-Canon drafts: BUILD-SEQUENCE, DECISIONS, CONTRACTS, CHECKLISTS, ARCHITECTURE,
  DELEGATE-GATE and BLOCKERS;
- ENV.md's rows that the Addendum has since amended;
- the `scaffold/` files.

Check this list before copying anything from them. **The right-hand column wins.**

Source codes: **C** = CANON.md, **A§n** = CANON-ADDENDUM.md section n.

**Missing bases.** The Addendum's "Supersedes" notes cite `kick_drafts secN code_block M` and
`kick_verify`. Those drafts are **not in this folder**; they appear only as quotations inside
`deliverables/*.docx`. Several files the Addendum edits have no base here: `apps/api/pyproject.toml`,
the root `pyproject.toml`, `docker-compose.yml`, `.github/workflows/*.yml` and
`packages/schemas/Makefile`. S1 writes them from scratch, using the Canon and Addendum values.

## Naming, paths, vocabularies

| Stale value (where) | Canonical | Source |
|---|---|---|
| `/v1` prefix (CHECKLISTS "API surface"); "no /v1/ prefix in MVP" (ARCHITECTURE "Explicitly out") | `/api/v1` | C |
| `/api/v1/health`, `/health` | `/healthz` (always 200, no DB), `/readyz` | C; semantics from BUILD S11 |
| `marketplace` singular | `marketplaces` | C |
| `jobs` table; `state` column; `COMPLETED`, `TIMEOUT`, `dead`, uppercase or Postgres ENUM (CONTRACTS enums.py, CHECKLISTS, BUILD S2, DELEGATE-GATE) | `job_runs.status`, TEXT + CHECK, values `queued\|running\|succeeded\|partial\|failed\|cancelled\|dead_letter`; a timeout is `error_code JOB_TIMEOUT` | C, A§18, A§20 |
| JobEnvelope/JobAccepted field `state:` (CONTRACTS jobs.py) | C says the column `status` "matches the JobEnvelope field", so treat it as `status`. **Confirm in S2.** | C |
| PipelineStage `loading_context, retrieving_brand, generating, merging, validating_*, scoring, awaiting_approval` (CONTRACTS enums.py, `scaffold/.../state.py`); "14 LangGraph node names" (BUILD S2) | 12 node names: `load_context … quality_check` | A§18 |
| `run_listing_graph(ctx)` (CONTRACTS, CHECKLISTS); `run_listing_pipeline(state: AgentState, deps)` (BUILD S14, DECISIONS D5) | `run_listing_pipeline(ctx: GenerationContext, deps: PipelineDeps) -> GenerationResult` | C |
| PipelineDeps with 8 ports including `load_rules`, `load_category_template`, `record_agent_run` (BUILD S14, D5, CHECKLISTS) | exactly 5 ports; context is pushed in `GenerationContext` | C, A§17 |
| AgentState in packages/schemas or "frozen" (CHECKLISTS) | AI-team internal, lives only in `packages/pipeline` | C |
| `packages/schemas/telemetry.py` / `record_agent_run()` for the AI team (BUILD S11, CHECKLISTS, DELEGATE-GATE) | deleted; the AI team returns `GenerationResult.agent_runs[]`; the backend's own model calls use `apps/api/app/services/telemetry.py` | C |
| `GenerationContext.marketplace_rules: list[dict]`, `category_template: dict` (CONTRACTS) | typed `RuleSet`, `CategoryTemplate` | C |
| Rule fields `field_path/kind/params`, RuleSet `version` (C AI-seam row) | `rules.py` is the only definition; RuleSet uses `rules_version` | A§16 |
| validation_runs.source `schema\|rules\|vision\|marketplace_api` (CONTRACTS validation.py, BUILD S7, CHECKLISTS) | all 7 ErrorSource values | A§19 |
| ErrorSource `api`, `backend`; the literal `"rules"` | `system`; `rules_engine` | C, A§19 |
| AI package home `apps/ai/`, `apps/api/app/agents/`, `app/workers/graph/` (CONTRACTS, CHECKLISTS, BUILD S1 tree) | `packages/pipeline` (dist `mkos-pipeline`); uv workspace members include it | C |
| Schemas module layout: 15 files, or `src/schemas/...` | 17 files, including `rules.py` and `export.py` | A§21 |
| `marketplace-os-schemas`, `@marketplace-os/schemas` | `mkos-schemas`, `@mkos/schemas` | C |
| `codes.py` / `paths.py` as written in CONTRACTS: no `HEADER_*`, `DUPLICATE_SKU_IN_FILE` or `JOB_TIMEOUT` codes; no `file.header` / `file.row` paths | add the 5 codes and 2 paths before the `schemas-v1.0.0` tag; `TIMEOUT` stays (`source=ai_model`) | A§20 |

## Generated types, CI, repo

| Stale value (where) | Canonical | Source |
|---|---|---|
| `apps/web/src/lib/api-types.ts`, `packages/schemas/generated/index.ts`, `packages/schemas/openapi.json`, `scripts/gen_ts_types.sh`, `gen:types` (BUILD S3, DELEGATE-GATE, CHECKLISTS, scaffold eslint) | `packages/schemas/jsonschema/openapi.json` plus `packages/schemas/ts/src/{api,contracts,index}`; `make -C packages/schemas all` | C, A§13, A§22, A§23 |
| `contracts.yml` with a `paths:` filter (CONTRACTS) | no `paths:` filter on any workflow carrying a required check | C |
| 9 required checks (C list, DELEGATE-GATE, D15) | **12**: add `bootstrap`, `lint-imports`, `gitleaks` | A§12 |
| Coverage `--cov-fail-under=70` from Day 2 (BUILD S1, D15, CHECKLISTS) | 0 in week 1 → 70 at Day 11 → +5 each Friday | C |
| CODEOWNERS with personal handles, `/apps/ai/`, two files (CONTRACTS) | one file, team handles only | C |
| `ENV: test` in CI jobs | the exact 4-variable env block (`APP_ENV=local`…, JWT_SECRET of 32+ chars) | A§11 |

## Database and migrations

| Stale value (where) | Canonical | Source |
|---|---|---|
| `0000_extensions` as an Alembic revision (BUILD S5, CHECKLISTS); `scripts/db/00-extensions.sql` | `scripts/db/0000_bootstrap.sql`, raw superuser SQL, idempotent, **not Alembic** | C, A§1–2 |
| `make dev` = compose up → uv sync → alembic upgrade head (BUILD S1); compose without an initdb mount | compose mounts `0000_bootstrap.sql` into `initdb.d`; root Makefile gets a `bootstrap-db` target; `make dev` runs it between `docker compose up -d` and `alembic upgrade head`; every CI job with Postgres runs the psql bootstrap step before its first Alembic call | A§1, A§2, A§7 |
| Rival revisions `0003_brand_chunks_hnsw`, `0003_auth_hardening`, `0003_contract_alignment` (CONTRACTS), `0003_jobs_and_idempotency`, `0006_job_runs`; RLS numbered 0004 or 0006 | ledger `0001, 0002, 0002b, 0003_ops_tables, 0004_integrity, 0005_rls` | C |
| "ten" tables in 0002 / "five" ops tables in 0003 | eleven / six | A§25 |
| rules `UNIQUE (marketplace, category, field, version)` | expression unique index with COALESCE (full DDL) | A§4 |
| audit_log monthly RANGE partitioning (BUILD S7, CHECKLISTS) | **not partitioned** for MVP | C |
| Cloud SQL Python Connector + IAM auth (CHECKLISTS) | URL + password (DATABASE_URL), Auth Proxy for laptops | C |
| `platform_scope` via session `execution_options` | `session.info[SCOPE_OK]`; `tenant_scope_checked` may appear in exactly 2 files | A§6 |
| Connection budget `(3 + 2) × 10 = 50`, `db-custom-1-3840` | `(3 + 2 + 2) × 10 = 70` of `max_connections=200` on `db-custom-2-7680` (**hosting under review**) | A§24 |

## Runtime, API, security

| Stale value (where) | Canonical | Source |
|---|---|---|
| Five ARQ queues as five worker services (BUILD S13, CHECKLISTS) | two: `q:default`, `q:publish` | C |
| Worker entrypoint bare `arq app.workers.settings.WorkerSettings` (BUILD S12, CHECKLISTS) | arq plus a health server on `$PORT`; module `python -m app.workers.main` | C (behaviour); the module name comes from the scaffold entrypoint. The Canon says aiohttp: see KNOWN-ISSUES |
| DLQ key `arq:dlq`; requeue mutates the failed row (CHECKLISTS) | requeue inserts a NEW `job_runs` row with `parent_job_id`; `dlq:{queue}` is only a counter | C |
| Rate limit 600/min per tenant (CHECKLISTS) | 120/min per tenant, 60/min per IP, 5/min on login | C |
| 50 MB body cap on `/products/bulk` (BUILD S10, CHECKLISTS) | 10 MiB multipart; larger files go through a signed upload URL, then `POST /api/v1/products/bulk` | C |
| JWT claim `role` (singular) or `tenant_id`; `POST /auth/token` (CHECKLISTS) | claims `{sub, tid, roles[], email, typ, jti, iss, aud, iat, exp}`; `POST /api/v1/auth/login` | C (D6) |
| "PATCH /content sets generated_by to the editor" (CHECKLISTS) | new version row; `generated_by_user_id` immutable; `edited_by_user_id` set | C |
| Admin kill switch reads `ENV` (BUILD S14, DELEGATE-GATE) | reads `APP_ENV`; exact test fixture | A§14 |

## Env register

| Stale value (where) | Canonical | Source |
|---|---|---|
| `ENV`, `ENVIRONMENT`, `SENTRY_ENVIRONMENT` | `APP_ENV` | C |
| `LLM_API_KEY` | `OPENAI_API_KEY` (plus an optional `DEEPSEEK_API_KEY`) | C |
| `GOOGLE_CLOUD_PROJECT`, `KMS_KEY_NAME`, `GOOGLE_APPLICATION_CREDENTIALS` SA key file | `GCP_PROJECT_ID`, `KMS_KEY_URI`, ADC login (**hosting under review**) | C |
| `LLM_*_BUDGET_INR` (D14, CHECKLISTS) | `LLM_DAILY_BUDGET_USD`, `LLM_TENANT_DAILY_BUDGET_USD` | C |
| `apps/api/.env.example` (BUILD "can start immediately") | a single `./.env.example` at the repo root | C |
| "80 rows"; ENV.md has no `PORT`, `DATABASE_POOL_TIMEOUT`, `DATABASE_POOL_RECYCLE`, `DB_STATEMENT_TIMEOUT_MS` rows | 83 rows; add those 4 | A§10, A§25 |
| ENV.md: `SENTRY_DSN` optional; `ADMIN_SESSION_SECRET`, `AMAZON_APP_ID` required everywhere | Sentry required when `APP_ENV != local`; admin secret defaulted locally; app id blank until approval | A§8, A§14 |
| Nine redirect URIs; `<domain>` placeholder | **six** URIs; placeholder token `<APEX>` | A§26 |

## Seed and fixtures

| Stale value (where) | Canonical | Source |
|---|---|---|
| `scripts/seed.py`, `uv run python scripts/seed.py`, profile `costs`, `SKU-001`, numpy vectors (BUILD S9, C seed row, CHECKLISTS, scaffold) | `python -m app.cli seed --profile demo\|isolation` (flag required); tenants `acme`/`bharat`; shared SKUs `KURTA-001, KURTA-002, DECOR-001`; plus `ACME-900` and `BH-900` in `demo` | A§5 |

## S1 scaffold (2026-09-29; ADR-0012, ADR-0013)

| Stale value (where) | Canonical | Source |
|---|---|---|
| BUILD S1: Python `3.11`, `pgvector/pgvector:pg16`, `redis:7-alpine`, an `arq` worker, `apps/web`, `packages/pipeline`, `apps/api/app/agents`, and "4 checks" green on a dummy PR (BUILD-SEQUENCE.md:6-20) | Python `==3.13.*`; `pgvector/pgvector:0.8.5-pg17` pinned by digest; no Redis and no worker in S1; a backend-only tree (`apps/api`, `packages/schemas`); all **10** required checks on every PR | ADR-0002, ADR-0003, ADR-0004, ADR-0012, ADR-0013 §4 |
| BUILD S1 "A force-push to main is rejected for an org admin" (BUILD-SEQUENCE.md:20) | moves to "when GitHub Team lands": GitHub Free has no rulesets on private repos, so the 10 checks are advisory and the user merges only when all are green | EXECUTION-PLAN.md:54, §4.3; ADR-0013 §5 |
| ENV.md `DATABASE_URL` local value `postgresql+asyncpg://…` | `postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os` (the root `.env.example`) | C (psycopg 3, ADR-0004) |
| ENV.md `GCP_PROJECT_ID` local value `aimos-dev` (no such project) | blank locally and optional until S4; the projects are `aimos-staging` and `aimos-prod` | C (hosting row), ADR-0001 |
| ENV.md `GIT_SHA` "Injected by Cloud Build" | set by the deploy pipeline (S4/S12); `dev` locally | ADR-0001 |
| A§7 `bootstrap` job: `alembic upgrade head`, `app.cli seed`, and the image import `import app, mkos_schemas, mkos_pipeline` | S1 runs the job without alembic (S5) or seed (S9), and the image import is `import app, mkos_schemas` until S14 | ADR-0013 §4 |
| A§11 CI env block with four variables (`postgresql+asyncpg` URL and `REDIS_URL`) | three variables: `APP_ENV`, `DATABASE_URL` (`postgresql+psycopg`), `JWT_SECRET` | C (ADR-0004 row) |
| A§1 compose `ports: ["5432:5432"]`; site L7004, L7020 `"9000:9000"`, L7021 `"9001:9001"` (every interface) | every published port binds loopback: `"127.0.0.1:<port>:<port>"`; `make dev` keeps uvicorn on its `127.0.0.1` default | ADR-0013; `docs/RULES.md` §9 |
| EXECUTION-PLAN decision 19: the agent-tooling folder listed as "Yes" for git (EXECUTION-PLAN.md:77) | agent tooling stays local through `.git/info/exclude` and is never pushed | STATE.md:188 |

## Pending edits to `scaffold/` (apply before moving the files in during S1)

| File | Edit | Source |
|---|---|---|
| `scaffold/README.md` | **Do NOT move it**, and never run `cp -R scaffold/.` here: it would overwrite the root README. Its "copy into the real repo" note is void, because this folder is the repo. Move the other files one by one, after their edits. | STATE, A§5 |
| `apps/api/app/db/session.py` | docstring allowlist → `app.admin.*`, `app.cli`, `scripts.*`; "the seed script" → `app.cli seed`; and fix the `SET LOCAL` bug (KNOWN-ISSUES C) | A§5, A§6 |
| `apps/api/app/core/config.py` | **Applied in S1**, as amended by ADR-0013 §1-2: `extra="forbid"`; the 10 fields; `otel_service_name="marketplace-os-api"`, the new `kms_local_dev_key`, `port=8000`. Not applied: the 3 FE keys (ADR-0013 §2), the admin-secret deploy guard (S14, ADR-0013 §1) and `arq_job_timeout_seconds` (no ARQ, ADR-0004). | A§8, A§9 |
| `apps/api/Dockerfile` | **Applied in S1**: `ENV UV_PROJECT_ENVIRONMENT=/opt/venv` in the builder stage | A§15 |
| `apps/api/docker-entrypoint.sh` | shell role → `exec python "$@"`: **applied in S1**. The seed role → `python -m app.cli seed --profile "${SEED_PROFILE:-demo}"` stays with S9 (the S1 entrypoint has only `api` and `shell`). | A§5, A§15 |
| `scripts/seed.py` | move the body into `apps/api/app/cli.py`; delete this file | A§5 |
| `packages/schemas/src/mkos_schemas/rules.py` | replace the RuleSet docstring (no `load_rules` port) and the Rule docstring (uniqueness is the A§4 COALESCE expression index) | A§4, A§17 |
| `packages/pipeline/src/mkos_pipeline/state.py` | `PipelineStage.LOADING_CONTEXT` → `LOAD_CONTEXT` | A§18 |
| `apps/api/app/services/imports/row_model.py` | `check_header() -> list[tuple[ErrorCode, str]]`; no f-string codes | A§20 |
| `apps/web/eslint.config.mjs` | remove the `src/lib/api-types.ts` ignore; set the `no-restricted-syntax` message to the A§22 text; add the api-types pattern group to `no-restricted-imports` (FE-owned) | A§22 |
