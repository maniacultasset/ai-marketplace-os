# Build sequence — S1 to S14

Dependency-ordered. Do not jump ahead: each step exists to unblock the one after it, and S2/S3 unblock the AI team and the frontend team simultaneously.


## S1 · Monorepo scaffold, toolchain and CI gates

**Owner:** Tech Lead (solo Days 2–10)  
**Depends on:** GitHub org + private repo + 6 GitHub Team seats (branch protection on a private repo is a PAID feature — see day-zero blocker). Nothing technical.


**What gets built**

Private monorepo `ai-marketplace-os/`: apps/api/app/{core,api/v1/routers,services,repositories,workers,adapters,agents}, apps/web, packages/schemas, packages/pipeline, infra, docs/adr, scripts, .github. DELIBERATE DEVIATION from the spec tree: nest code under `apps/api/app/` so it is an importable package (`from app.core.config import settings`) — without a package root, mypy, pytest rootdir and the Docker COPY layer are all ambiguous. Root pyproject.toml = uv workspace (members apps/api, packages/schemas), `requires-python = "==3.11.*"` mirrored in .python-version, python:3.11-slim and the CI matrix. ruff lints only (line-length 100, E/F/W/I/N/UP/B/C4/SIM/ASYNC/S/T20/RUF, E501 ignored), black formats (100), mypy strict + pydantic.mypy plugin (do NOT add the SQLAlchemy mypy plugin — 2.0 types natively via Mapped[]), pytest asyncio_mode=auto, --strict-markers, --cov-fail-under=70. pre-commit: ruff --fix, black, gitleaks, detect-private-key, check-added-large-files 512kb, local mypy. docker-compose.yml: pgvector/pgvector:pg16, redis:7-alpine, minio + a one-shot `mc` container creating buckets `marketplace-images` and `marketplace-csv` (the spec's compose creates none, so the first upload 404s); healthchecks on all three; no obsolete `version:` key. Root Makefile: `make dev` = compose up, uv sync, alembic upgrade head, seed, uvicorn --reload :8000, arq worker, pre-commit install. .github/workflows/ci-api.yml with FOUR SEPARATE JOBS — api-lint, api-typecheck, api-tests, tenant-isolation — because GitHub required checks are per-job and a step inside another job can be silently removed. CODEOWNERS + branch protection on main (1 approval + CODEOWNERS, 2 for packages/schemas and alembic/, linear history, squash-only, include administrators).


**Unblocks:** Every line of code all three teams write. This is the Day-2 Definition of Done.


**Done when:** On a clean machine: `git clone && cp .env.example .env && make dev` boots postgres+redis+minio, applies migrations, serves http://localhost:8000/docs and runs an arq worker in under 10 minutes with zero Slack questions. A dummy PR shows api-lint, api-typecheck, api-tests, tenant-isolation green. A force-push to main is rejected for an org admin.


## S2 · Freeze packages/schemas v1.0.0 — the cross-team contract

**Owner:** Tech Lead (solo); becomes a 3-team CODEOWNERS review surface from D11  
**Depends on:** S1. Decisions D1, D2, D3, D7, D8 must be settled first — all are Day-2/Day-3 calls, none need the client.


**What gets built**

Make `packages/schemas` a real installable distribution (dist `mkos-schemas`, import `mkos_schemas`, hatchling, single dep pydantic>=2.9,<3), installed with `uv pip install -e ./packages/schemas` — not a loose folder, or the AI team copy-pastes shapes into LangGraph nodes. Freeze eight contracts: base.py (StrictModel extra=forbid for inbound HTTP bodies; WireModel extra=ignore for cross-team payloads so a producer can ship an additive MINOR field before consumers upgrade); enums.py (ListingStatus = the 9 lowercase DDL values, PipelineStage = the 14 LangGraph node names, JobState = QUEUED|RUNNING|SUCCEEDED|PARTIAL|FAILED|CANCELLED|DEAD_LETTER, Role, Marketplace); codes.py (one ErrorCode + ImageIssueCode registry — FIELD_TOO_LONG, never TITLE_TOO_LONG); paths.py (dotted field paths `content.title`, `content.bullets[2]`, `keywords.hidden_terms`, `attributes.color`, `images[0]`, literal `listing`, plus `resolve_field_path()`); listing.py (CanonicalListing, NESTED shape, `keywords.hidden_terms` not `backend_keywords`, structural caps only — title ≤500, 1–10 bullets, description ≤5000; marketplace limits live in the `rules` table, never in Pydantic, or the Rules Engine never runs and the AI gets a 422 instead of a fixable ErrorReport); errors.py (spec [121] shape + three additions: `severity` on each item, `api_error_detail` on each item, `request_id`+`listing_id` at top level); images.py (ImageReport + `checks[]` proving which checks ran; `pass_: bool = Field(alias='pass')` because `pass` is a keyword, and `protected_namespaces=()` because `model_used` collides with Pydantic v2's `model_` namespace); agent.py (AgentRunRecord); validation.py; jobs.py (JobAccepted, JobEnvelope, GenerationContext, GenerationResult with `blocks[]` for partial agent success); publish.py (ListingResult/FieldResult, per-field never a boolean); imports.py (ImportReport reusing ErrorItem verbatim); auth.py (JWTClaims). Golden fixtures in fixtures/*.json. Semver policy: MINOR = additive optional field, any day; MAJOR = rename/removal/type change, only in the Day-14 / Day-28 / Day-42 milestone windows.


**Unblocks:** The AI team and the FE team, completely and simultaneously. This is the highest-leverage item in the entire pack — it feels like paperwork and it is the thing that lets two other teams start.


**Done when:** In a bare venv, `pip install -e packages/schemas && python -c 'from mkos_schemas.listing import CanonicalListing'` works. Every one of the eight contracts has a golden fixture that round-trips through model_validate → model_dump. `model_json_schema(mode='serialization')` emits `pass`, not `pass_`. Tag `schemas-v1.0.0` pushed, and the written contract note (final column names, the status/stage split, node-event strings, job polling shape) is in the AI team's and FE team's hands.


## S3 · Freeze the /api/v1 surface as OpenAPI, generate TS types, stand up a mock

**Owner:** Tech Lead (solo) → BE from D11  
**Depends on:** S2. Decision D4 (/api/v1 prefix).


**What gets built**

Mount everything under `/api/v1` (`app.include_router(v1_router, prefix='/api/v1')`); /healthz, /readyz, /openapi.json, /docs stay unversioned. Every planned endpoint exists TODAY as a stub raising 501 but annotated with the correct `response_model`, so /openapi.json is complete and accurate on Day 3 with nothing wired: auth (login, refresh, logout, me); products + products/bulk; imports/{id}, imports/{id}/rows?status=failed, imports/{id}/rows/retry; listings generate|validate|approve|publish, PATCH listings/{id}/content, GET listings/{id}/status (alias), POST listings/{id}/publish/retry-fields; marketplaces connect|callback|accounts; images upload-url|upload|{id}/report; rules list|dry-run; jobs/{job_id}, jobs/dead-letter, jobs/dead-letter/{id}/requeue; analytics summary|timeseries|marketplace-breakdown|costs (GET /api/v1/analytics kept as an alias for summary). Two standard envelopes, applied everywhere: errors are always `{"error":{type,message,status,correlation_id,details,error_report}}` via two exception handlers plus a RequestValidationError override; lists are always `{"data":[...],"page":{next_cursor,has_more,limit}}` with a base64 keyset `(created_at, id)` cursor, never OFFSET. Non-obvious rule to state now: `POST /api/v1/listings/validate` returns **200** with `{"passed": false, "error_report": {...}}` when rules fail — validation ran fine, the listing is what is invalid; a 422 there forces the FE to write two error paths for one screen. `scripts/gen_ts_types.sh` dumps openapi.json into packages/schemas and runs openapi-typescript into apps/web/src/lib/api-types.ts; both committed, marked linguist-generated, never hand-edited. CI job `schemas-drift` regenerates and fails on `git diff --exit-code`. `make mock` runs `prism mock packages/schemas/openapi.json --port 4010`.


**Unblocks:** FE team builds real screens against generated types and a live mock from Day 11 without a single working handler. AI team gets POST /api/v1/rules/dry-run, turning their prompt-tuning loop from 30 seconds into 200ms for three weeks.


**Done when:** `curl localhost:8000/openapi.json | jq '.paths|keys|length'` returns the full frozen surface; the schemas-drift job fails on a hand-edited api-types.ts; prism answers GET /api/v1/products with fixture data; FE confirms in writing they import api-types.ts and hand-write no response interfaces.


## S4 · GCP landing zone and data plane

**Owner:** Tech Lead (solo); OPS owns from D11  
**Depends on:** Client GCP organisation + verified billing account (day-zero blocker). Decision D9 (region).


**What gets built**

Three projects under one folder — aimos-dev, aimos-staging, aimos-prod (separate projects is the cleanest way to satisfy the plan's 'Dual Environment Isolation' invariant and gives separate billing lines). Region **asia-south1 (Mumbai)** for Cloud Run, Cloud SQL, Memorystore, GCS and Artifact Registry — no spec names a region anywhere in 233 specs, and the region cannot be changed later without recreating Cloud SQL. Enable the Day-1 API list (run, sqladmin, secretmanager, cloudbuild, artifactregistry, storage, redis, cloudtrace, logging) PLUS the four it omits and which break Day 3 and Day 30: cloudkms, vpcaccess, servicenetworking, iamcredentials+sts. Cloud SQL PostgreSQL 16 with private IP; **automated backups and point-in-time recovery enabled at instance creation** — retro-fitting PITR needs a restart and does not retroactively create WAL, and Day 43 rehearses a restore with a <15 minute target; maintenance window outside IST business hours; log_min_duration_statement=500ms; Query Insights on. Memorystore Redis Basic 1GB. GCS buckets (images, csv-imports) with lifecycle rules. Serverless VPC connector. Secret Manager seeded per environment with DATABASE_URL, REDIS_URL, JWT_SECRET, OPENAI_API_KEY, SENTRY_DSN, AMAZON_CLIENT_ID/SECRET, FLIPKART_CLIENT_ID/SECRET (placeholders until the applications clear) — JWT_SECRET must differ per environment or a staging token works in production. KMS keyring `marketplace-os` + key `oauth-token-key`, 90-day rotation, `roles/cloudkms.cryptoKeyEncrypterDecrypter` to the api service account only. All IAM granted to Google Groups (gcp-aimos-backend@, gcp-aimos-ai@, gcp-aimos-fe@), never individual emails; the AI group gets **no** secretAccessor on AMAZON_*/FLIPKART_* — with a separate AI team, invariant #6 stops being a code-structure concern and becomes an access-control one. Budget alerts at 50/90/100% on each project.


**Unblocks:** Everything from S5 onward that touches a real database; the Day-7 staging deploy; Day-30 KMS token encryption; keyless GitHub Actions deploys.


**Done when:** From a laptop holding only roles/cloudsql.client: `cloud-sql-proxy --port 5433 aimos-staging:asia-south1:aimos-pg` connects and `SELECT extversion FROM pg_extension WHERE extname='vector'` returns ≥0.8 (0.8+ is needed for iterative index scans on filtered vector search). `gcloud kms keys list` shows oauth-token-key. A test budget threshold fires an alert email. Automated backups + PITR visible on the instance.


## S5 · Alembic async wiring + migration 0000_extensions + DB roles

**Owner:** Tech Lead (solo) → DB/BE from D11  
**Depends on:** S4 (Cloud SQL exists), S1.


**What gets built**

Four things that must be right on Day 3 or every later migration is hand-written: (1) a `naming_convention` on MetaData — Alembic cannot autogenerate a DROP for an unnamed constraint and you will need to drop one; (2) async env.py using `connection.run_sync(do_run_migrations)` over asyncpg; (3) `compare_type=True, compare_server_default=True, transaction_per_migration=True` (the last one is what lets a later CREATE INDEX CONCURRENTLY run via `op.get_bind().execution_options(isolation_level='AUTOCOMMIT')` — write it into the migration template now, not during a prod deploy on Day 44); (4) a `render_item` hook so autogenerate emits `pgvector.sqlalchemy.Vector(1536)` AND writes the import — without it autogenerate silently produces a migration that fails at runtime on the embedding column. Migration 0000: `CREATE EXTENSION vector, pgcrypto, pg_trgm`; create role `migrator` (owns the schema) and role `app_rw` (what the API and ARQ workers connect as) plus default privileges — this split cannot be retrofitted once tables exist, and RLS in week 2 is silently bypassed by the table owner and by the default Cloud SQL `postgres` superuser. Engine config: `create_async_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=1800)` — pool_recycle because Cloud SQL drops idle connections and asyncpg surfaces that as a mid-request InterfaceError; total connections = (api max-instances + worker max-instances) × 10, so set `--max-instances` explicitly on both Cloud Run services and put the arithmetic in an ADR. Add `alembic check` and an upgrade-head→downgrade-base round trip against a throwaway pgvector container to CI.


**Unblocks:** Every migration after it, and the entire repository layer.


**Done when:** `alembic upgrade head` applies to both local docker Postgres and staging Cloud SQL; `alembic downgrade base && alembic upgrade head` round-trips clean in CI; `alembic check` is a required status check that fails when a model and the migration head drift; autogenerate on a model carrying a Vector column produces a migration that actually runs.


## S6 · Migration 0001_core_tenancy

**Owner:** Tech Lead (solo) → DB from D11  
**Depends on:** S5. Decisions D1 (denormalised tenant_id) and D2 (one column name) MUST be settled before a line of this is written — backfilling tenant_id onto a populated listings/audit_log table plus rewriting every FE and AI query is a multi-day rework.


**What gets built**

`tenants`, `users`, `products`, plus the deltas the spec DDL omits: `UNIQUE (tenant_id, sku)` on products (the spec writes `UNIQUE (brand_id, sku)` — see D2); `users` email uniqueness as `(tenant_id, lower(email))` or CITEXT; `users.is_active BOOLEAN NOT NULL DEFAULT true`; `CHECK (hashed_password IS NOT NULL OR auth_provider IS NOT NULL)` so a NextAuth OAuth user cannot create a login-less ghost row; `products.updated_at` plus a shared `set_updated_at()` trigger function reused by every table; and the composite-FK anchors `UNIQUE (id)` on tenants and `UNIQUE (tenant_id, id)` on users and products — these are what make 0002's composite foreign keys possible and they cost one index each at MVP scale. Matching Pydantic v2 create/read/update models live in packages/schemas, not in apps/api.


**Unblocks:** 0002, the repository layer, auth, seed data. Nothing in the data layer moves until this lands.


**Done when:** Migration applies to staging Cloud SQL; a unit test proves a duplicate SKU inside one tenant is rejected and the identical SKU in a second tenant is accepted; Pydantic round-trip tests pass.


## S7 · Migration 0002_listing_pipeline (+ 0002b HNSW index)

**Owner:** Tech Lead (solo) → DB from D11  
**Depends on:** S6. Decision D12 (HNSW not IVFFlat; 1536 dims pinned).


**What gets built**

The ten remaining tables with the corrections the spec DDL is missing — cheap today, a coordinated three-team rework in week 3. (a) `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE` on ALL SIX orphan tables — listings, listing_versions, validation_runs, agent_runs, performance, audit_log — held consistent by composite FKs such as `FOREIGN KEY (tenant_id, listing_id) REFERENCES listings (tenant_id, id)`, which makes drift impossible at the DB level rather than by convention. (b) `rules` stays platform-global (the ER diagram says tenant-scoped, the DDL says global — DDL wins) with a NULLABLE tenant_id reserved for Phase-2 overrides and `UNIQUE (marketplace, category, field, version)`. (c) audit_log becomes `(id, tenant_id, actor_user_id, actor_type, actor_email, action, entity_type, entity_id, before, after, request_id, occurred_at)` — the spec collapsed entity_type+entity_id into one `entity TEXT` (so you cannot answer 'show me everything that happened to listing X', the only question an audit log is ever asked) and named a column `timestamp`, which is a Postgres type name; `actor_type` and nullable `actor_user_id` with `CHECK (actor_type='system' OR actor_user_id IS NOT NULL)` because most writes come from the worker and the agents; monthly RANGE partitioning on occurred_at so retention is DROP PARTITION rather than a DELETE the append-only trigger blocks. (d) agent_runs gains tenant_id, listing_id, listing_version_id, request_id, job_id, agent_name, attempt, prompt_version, started_at, finished_at, and `cost` → `cost_usd NUMERIC(12,6)` (DECIMAL(10,6) caps at 9999.999999) — without tenant_id and agent_name, spec [172]'s cost-per-tenant and cost-per-agent are literally uncomputable and embedding runs (product_id is NULL) are untenantable. (e) validation_runs source enum becomes `schema|rules|vision|marketplace_api` (both spec versions omit `schema` although [120] makes Pydantic stage 1) plus tenant_id, listing_version_id, attempt, duration_ms, rules_version, request_id. (f) images merged from its two conflicting definitions into `(id, tenant_id, product_id, storage_key, slot, marketplace, status, compliance_status, image_report_json, metadata, created_at)` — `slot` is load-bearing for Amazon main-image rules; storage_key is a GCS object key, URLs are signed at read time and never stored; `products.images JSONB` is demoted to raw-CSV-input-only. (g) performance gains tenant_id, marketplace, ranking_position, created_at and `UNIQUE (listing_id, marketplace, date)` so daily ingestion is an idempotent upsert instead of a duplicate generator. (h) marketplace_accounts gains seller_id, region, scopes, expires_at, last_refresh_at, kms_key_version, connected_by_user_id — without expires_at the Day-30 'refresh before expiry' job is unimplementable and without kms_key_version a 90-day key rotation breaks decryption. (i) brand_chunks gains embedding_model, content_hash, source_type, source_id, chunk_index, token_count, is_active. (j) Indexes: products(tenant_id, created_at DESC); listings(tenant_id, status, marketplace) + UNIQUE(product_id, marketplace); listing_versions UNIQUE(listing_id, version); validation_runs(listing_id, created_at DESC); agent_runs(tenant_id, created_at DESC) and (tenant_id, model_used, created_at DESC); audit_log(tenant_id, occurred_at DESC) and (entity_type, entity_id, occurred_at DESC); images(product_id), (tenant_id, status); NO speculative GIN on products.attributes. Separate migration 0002b holds only `CREATE INDEX brand_chunks_embedding_idx ON brand_chunks USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)` so it can be dropped, retuned and rebuilt without touching the pipeline schema.


**Unblocks:** The repository layer, the AI team's brand_chunks retrieval, every write path in the system, and every observability query. The observability columns in (d) and (e) must land HERE — retrofitting them after three teams have written call sites is the expensive version.


**Done when:** Migration applies clean up and down; a test asserts every table inheriting the TenantScoped mixin has a NOT NULL tenant_id and an index on it; inserting a listing_version whose tenant_id disagrees with its parent listing is rejected by the composite FK; `EXPLAIN` on a top-8 cosine query shows the HNSW index in use.


## S8 · Migration 0003_ops_tables — the five tables the specs do not have

**Owner:** Tech Lead (solo) → BE from D11  
**Depends on:** S7.


**What gets built**

None of these exist in the 13-table DDL, and each is a cross-team contract rather than an internal detail. `job_runs (id, tenant_id, kind, status, node, attempt, arq_job_id, listing_id, entity_type, entity_id, idempotency_key, payload_json, result_json, error_report_json, request_id, queued_at, started_at, finished_at)` with `UNIQUE (tenant_id, idempotency_key)` — the API returns 202 + job_id and the FE polls it, but ARQ keeps results only in Redis at `arq:result:<id>` under a keep_result TTL (default 3600s) and Memorystore Basic has no persistence and no failover, so a restart leaves the FE polling a 404 forever; status uses the frozen JobState enum including PARTIAL, which is load-bearing for invariant 12. `imports (id, tenant_id, gcs_uri, filename, total_rows, imported_rows, failed_rows, status, job_id, created_by, created_at)` and `import_rows (id, import_id, tenant_id, row_number, raw_row JSONB, status, product_id, error_report_json)` — without these the Day-8 DoD ('50-row CSV with 5 bad rows returns 45 imported and 5 row-level errors') and the Day-13 failed-row re-run endpoint cannot be built. `idempotency_keys (tenant_id, key, endpoint, request_hash, response_status, response_json, created_at)` backing a REQUIRED `Idempotency-Key` header on POST /listings/publish and POST /products/bulk: same key + same body replays the stored response, same key + different body returns 409. `refresh_tokens (id, tenant_id, user_id, token_hash, expires_at, revoked_at, replaced_by, user_agent, ip, created_at)` storing sha256(token) only, with `replaced_by` giving rotation-reuse detection — without it there is no logout, no revoke-on-compromise and nothing to show the Day-41 audit. Plus `listing_versions.edited_by_user_id` (added now even if the client later relaxes the rule, because adding it mid-sprint is a migration) and `category_templates (marketplace, category, attributes JSONB, external_category_id)`, which the Attribute Agent and Day-20's 'canonical attribute-mapping table' both need and which the `rules` table cannot serve.


**Unblocks:** The FE team's single job-polling hook covering all four job kinds; the import-review UI and row-error drawer; auth refresh/logout; the AI team's Attribute Agent category schema.


**Done when:** `GET /api/v1/jobs/{job_id}` returns a JobEnvelope read from Postgres with Redis deliberately stopped; a 50-row CSV with 5 bad rows yields imports.failed_rows=5 and 5 import_rows carrying populated error_report_json; replaying a publish with the same Idempotency-Key returns the stored response, and with a different body returns 409.


## S9 · TenantScopedRepository + query guard + cross-tenant CI gate + two-tenant seed

**Owner:** Tech Lead (solo) → BE + DB from D11  
**Depends on:** S7, S8.


**What gets built**

The spec's base class is a good start but bypassable — nothing stops `session.execute(select(Listing))`. Three parts: (1) a `TenantScoped` declarative mixin providing the tenant_id column, so 'is this table tenant-owned' is a type check rather than a naming convention; (2) `TenantScopedRepository[ModelT]` whose `__init_subclass__` refuses a repository for a non-TenantScoped model and whose `_stmt()` is the only select factory, stamping `.execution_options(tenant_scope_checked=True)`; (3) a SQLAlchemy `do_orm_execute` event listener raising `CrossTenantQueryError` when any mapper in a statement is TenantScoped and that execution option is absent — bypassing is then still possible but loud and greppable, because you have to type the flag yourself. tenant_id comes from the JWT `tid` claim and from nowhere else: no endpoint ever reads it from a body, query string or header. Ship `BrandChunkRepository.search(query_embedding, k=8, types=None)` as the single method the AI team calls, so nobody hand-writes the filtered vector SQL: HNSW applies the tenant filter AFTER the index scan, so a small tenant can return fewer than 8 chunks — mitigate with pgvector 0.8+ `SET LOCAL hnsw.iterative_scan='relaxed_order'`, and make the Day-17 benchmark multi-tenant (50 tenants × 100 chunks), because a single-tenant 5,000-chunk benchmark will not surface this at all. `scripts/seed.py` (`make seed`) creates TWO tenants with OVERLAPPING SKUs (both hold SKU-001, proving the unique is tenant-scoped rather than global) and one user per role per tenant (you cannot test maker-checker with one user); it must run with NO LLM key and NO GCP credentials — hash-seeded deterministic vectors for embeddings, MinIO for images — or it blocks every dev who does not yet have keys. Seeding also exports packages/schemas/fixtures/*.json for FE mocks and AI golden tests. `tests/test_tenant_isolation.py` runs as its own CI job named `tenant-isolation`, covers relational reads AND pgvector similarity search, and loops over every mapper inheriting TenantScoped so a table added in week 4 cannot silently skip coverage. RLS lands in week 2 as migration 0006 (defence in depth against raw SQL, worker code and LangGraph nodes that will not all go through our repositories), behind a settings flag, using `SET LOCAL app.tenant_id` inside a transaction — never plain `SET`, which leaks tenant A's GUC into tenant B's next request on a pooled connection.


**Unblocks:** Every service and every worker. It is also the gate that makes it safe to hand DB-touching tickets to developers who did not design the schema.


**Done when:** The cross-tenant test is a required status check and FAILS when the tenant filter is deliberately removed; `make seed` produces 2 tenants × 3 products with colliding SKUs and ≥2 users per tenant on a machine with no GCP or LLM credentials; a raw `select(Listing)` without the scope flag raises CrossTenantQueryError.


## S10 · Auth, RBAC, maker-checker and audit (+ migration 0004_integrity)

**Owner:** Tech Lead (solo) → BE from D11  
**Depends on:** S6, S8 (refresh_tokens). Decision D6 (who issues the JWT) and the two client rulings in D13.


**What gets built**

FastAPI is the SOLE token issuer. `POST /api/v1/auth/login` validates email+password (argon2id via pwdlib) and returns `{access_token, refresh_token, expires_in, user}`; `/refresh` rotates with reuse detection (presenting an already-rotated token revokes the whole chain); `/logout` revokes; `/me` returns the Principal. HS256 with JWT_SECRET from Secret Manager, access 30 min, refresh 14 days. Claims frozen as `{sub, tid, roles[], email, typ, jti, iss, aud, iat, exp}` — `roles` is always an array of length one so the token shape never has to change when multi-role arrives. Use PyJWT[crypto], NOT python-jose (unmaintained, algorithm-confusion CVEs); pwdlib[argon2], NOT passlib (last release 2020, breaks against bcrypt≥4.1). `get_current_principal` is a FastAPI DEPENDENCY, not middleware — per-route, typed, testable, and it lets /healthz and the OAuth callbacks be unauthenticated without an exclusion regex; middleware is reserved for request_id only. `core/rbac.py` holds Role (owner|operator|reviewer|admin) × Action (product:import, listing:generate, listing:regenerate, listing:edit, listing:approve, listing:publish, marketplace:connect, team:manage, settings:manage, analytics:read) as an executable matrix, with a test asserting every route in `app.routes` carries either `require_role` or an explicit `@public` marker. Maker-checker enforced in BOTH places: the approve service loads the LATEST listing_version and rejects with 403 + `MAKER_CHECKER_VIOLATION` when the approver generated or edited it (approval always targets the latest version — say so explicitly or a race lets someone approve a superseded one), and it IGNORES any client-supplied `reviewer_id` (spec [108] has the client send it; trusting it is an impersonation hole — the approver is `principal.user_id`, full stop). Migration 0004 makes the DB the backstop the worker and the AI team cannot bypass: a trigger making generated_by_user_id, content_json and the approval immutable once written, requiring the approver to exist, be in the same tenant and hold reviewer or owner; `generated_by_user_id` becomes NOT NULL populated with the HUMAN who clicked Generate, because the spec's CHECK passes trivially when it is NULL — which is precisely the AI-generation case, making the shipped constraint decorative. Same migration makes audit_log append-only for real: REVOKE UPDATE, DELETE from app_rw plus a BEFORE UPDATE/DELETE/TRUNCATE statement trigger. One `audit()` helper used by every state-changing service, logging auth events (login, failed login, refresh reuse, role change) alongside business actions. OAuth connect gets a Redis-backed `state` CSRF token (OAUTH_STATE_TTL_SECONDS=600) plus PKCE where supported — specs [110]/[159] never mention state, and without it any site can connect a marketplace into a logged-in user's tenant. Rate limits: 120/min per tenant, 60/min per IP, 5/min on login per IP+email; REQUEST_MAX_BODY_BYTES=10485760 with /products/bulk exempted to 50MB; ALLOWED_ORIGINS is an explicit list, never '*'.


**Unblocks:** Every protected endpoint the FE builds; the walking skeleton; the approval screen the FE starts in week 5. Also closes the single-user-tenant deadlock: the DB CHECK is role-blind, so a one-user tenant — even an Owner — can never approve anything, which would break the Day-44 UAT demo.


**Done when:** Login issues a JWT carrying the frozen claims; a reviewer-only token gets 403 on POST /listings/generate; refresh rotation revokes the chain on reuse; a generator approving their own listing is rejected at the API with a well-formed ErrorReport AND by the DB trigger when the API is bypassed; `UPDATE audit_log` fails for app_rw; the permission matrix is in the FE team's and AI team's hands.


## S11 · Observability spine — correlation IDs before the first worker exists

**Owner:** Tech Lead (solo) → OPS + BE from D11  
**Depends on:** S7 (the observability columns in agent_runs/validation_runs), S1.


**What gets built**

The plan sequences observability at step 21 of 22 (around Day 38). The exporters can wait there; the IDs and the columns cannot, because retrofitting them means touching every call site after three teams have written against them. Ship now: `asgi-correlation-id`-style middleware reading X-Request-ID or minting a UUIDv7, stored in a contextvar, echoed in the response header, bound into structlog, passed EXPLICITLY in every ARQ job payload and re-bound in the worker's `on_job_start`, stamped onto job_runs, agent_runs, validation_runs and audit_log, and set as OTel span attribute `request_id`. structlog emitting one JSON object per line to stdout with Cloud Logging field names (severity, message, request_id, tenant_id, `logging.googleapis.com/trace`, spanId) — on Cloud Run stdout IS the transport; add no logging agent and no OTLP collector for logs. A central redaction processor plus a Sentry `before_send` scrubber with a deny-list: token_enc, access_token, refresh_token, hashed_password, authorization, OPENAI_API_KEY, AMAZON_CLIENT_SECRET, FLIPKART_CLIENT_SECRET, x-amz-access-token; `send_default_pii=False`. `GET /healthz` returns 200 unconditionally with no DB touch (a liveness probe that hits Cloud SQL cycles every instance on a brief blip); `GET /readyz` pings Postgres and Redis and returns 503 naming the failed dependency; both excluded from OTel and access logs; the worker gets a tiny HTTP server on the same container for its own probe. `packages/schemas/telemetry.py` ships `record_agent_run()` as an async context manager the AI team wraps every model call in, setting model/tokens_in/tokens_out on the yielded usage object — they never INSERT into agent_runs themselves, which makes invariant 7 structurally guaranteed rather than remembered, and puts the model price table (`pricing.py`, model id → USD per 1M tokens with effective_from) in our repo so a price change needs no AI-team deploy. Cost is computed at write time and STORED, never recomputed later. Sentry (FastApiIntegration, SqlalchemyIntegration, ArqIntegration, RedisIntegration; environment + release=GIT_SHA) and OpenTelemetry exporting DIRECT to Cloud Trace via opentelemetry-exporter-gcp-trace — note the spec's `.env.example` sets OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317, implying a collector sidecar that the plain-Cloud-Run deployment model does not have; keep that var for a local Jaeger container only.


**Unblocks:** The AI team's telemetry contract (they can write cost rows on Day 11 without touching SQLAlchemy); Day-40's DoD that a single failed publish is traceable from button click to marketplace response; every debugging session all three teams have for the next six weeks.


**Done when:** A request with `X-Request-ID: abc` produces that id in the response header, in every backend log line, in the enqueued job payload, in the worker's log lines and on the agent_runs row; a log line containing a fake AMAZON_CLIENT_SECRET comes out redacted; /healthz and /readyz are wired to the Cloud Run startup and liveness probes; `record_agent_run` is documented in ONBOARDING.md with a copy-pasteable example.


## S12 · Walking skeleton live on staging Cloud Run

**Owner:** Tech Lead (solo); OPS owns CD from D11  
**Depends on:** S4, S5, S6, S9, S10, S11. Client domain + DNS delegation (day-zero blocker) — needed in hand by Day 6, because DNS can take 48h and the managed cert another 15–60 min after records resolve.


**What gets built**

The first real vertical slice through the layering: products CRUD implemented router → service → repository with nothing skipped, proving the rule that `api/v1/routers/**` may not import sqlalchemy, app.models or app.db (enforced by ruff flake8-tidy-imports banned-api plus an import-linter layered contract — without the CI check this rule decays by Week 3, it always does). ONE multi-stage Dockerfile for apps/api used for BOTH Cloud Run services, different entrypoints only: `gunicorn -k uvicorn.workers.UvicornWorker app.main:app` for api, `arq app.workers.settings.WorkerSettings` for the worker (worker at min-instances=1). Cloud Build → Artifact Registry → `gcloud run deploy api-staging` / `web-staging` with `--set-secrets` from Secret Manager, keyless via Workload Identity Federation. **Migrations run as a Cloud Run job before the deploy, never at container startup.** Custom staging domain with a Google-managed certificate (api-staging.<domain>, app-staging.<domain>). Auto-deploy on push to main, which is also what the daily 05:00 PM 'Staging Deploy & Check' rhythm depends on.


**Unblocks:** The FE team pointing at a real staging API instead of only prism; every later deploy; the Day-7 DoD ('a teammate can log into staging and create a product over TLS').


**Done when:** A second person logs into https://app-staging.<domain> over TLS, creates a product, and it appears in staging Cloud SQL under the right tenant; a push to main auto-deploys both services and runs the migration job first; CI fails a PR that imports sqlalchemy inside a router.


## S13 · Async job spine + CSV bulk import as the first real job

**Owner:** Tech Lead (solo) → BE + OPS from D11  
**Depends on:** S8 (job_runs, imports, import_rows, idempotency_keys), S11 (request_id into the worker), S12.


**What gets built**

Five ARQ queues as separate worker deployments so a 1,000-row import cannot starve a publish and per-marketplace concurrency is bounded independently of LLM concurrency: `q:generation` (max_jobs 10, timeout 300s), `q:publish` (4, 600s), `q:images` (8), `q:imports` (2, 1800s), `q:embeddings` (4). The ordering rule the specs never state and which is the single most common bug in this shape of system: open transaction → write domain rows + the job_runs row (QUEUED) → COMMIT → `pool.enqueue_job(..., _job_id=job_id)`. Never enqueue inside the transaction, or the worker dequeues before the commit lands and reads a row that does not exist. Add an ARQ cron every 60s re-enqueuing job_runs stuck in QUEUED for >2 minutes with no ARQ job, covering the crash-between-commit-and-enqueue window — ~30 lines, same guarantee as a full transactional outbox. Retry policy by failure class: transport/5xx/429 at the ARQ level, max 5 attempts, full-jitter backoff `random.uniform(0, min(60, 2**(attempt-1)))`; agent correction capped at 3 INSIDE the graph edge logic and NOT by ARQ re-runs (generation jobs get `max_tries=1` for domain failures, or ARQ turns a bounded 3 into an unbounded 15 and breaks invariant 9); auth failure = exactly one automatic token refresh then surface AUTH_EXPIRED with retryable=false; non-retryable domain failures never re-enter the queue. Build the dead-letter queue ourselves — ARQ has none, it records a failed result after max_tries and stops, while specs [138]/[139] both assume one exists: on terminal failure write `job_runs.status=DEAD_LETTER` keeping the full payload, attempt history and error_report_json, push the id to a Redis list `dlq:{queue}` for the counter, expose `GET /api/v1/jobs/dead-letter` and `POST /api/v1/jobs/dead-letter/{id}/requeue` which creates a NEW row with a fresh correlation_id linked by `parent_job_id` (resetting the old row destroys the audit trail of what failed and when). First real job: `POST /api/v1/products/bulk` → GCS/MinIO → header validation → stdlib `csv` + per-row Pydantic validation (you do NOT need pandas; row isolation is exactly what per-row Pydantic gives) → imports/import_rows → ImportReport. Worker logs `event=queue.snapshot` every 30s with queue, depth, oldest_age_s, dlq_size — ARQ exposes no metrics endpoint, and oldest-job AGE is the only meaningful queue alarm (depth legitimately spikes to 1000 on the Day-42 load test).


**Unblocks:** The AI team's generation job (their pipeline runs inside this worker), the FE team's job-status UI and row-error drawer, and every long-running operation in the product.


**Done when:** A 50-row CSV with 5 malformed rows returns 45 imported products and 5 row-level errors rather than a batch crash; re-uploading the same CSV creates no duplicate SKUs; killing the worker mid-job leaves a recoverable job_runs row that the sweeper re-enqueues; a forced terminal failure produces a DEAD_LETTER row and a requeue creates a new job with parent_job_id set.


## S14 · Rules Engine, the AI seam, the internal admin, and ONBOARDING.md

**Owner:** Tech Lead (solo) → BE owns rules, OPS owns admin deploy from D11  
**Depends on:** S2, S3, S9, S13.


**What gets built**

Four things that together constitute 'other developers can now be handed tickets'. (1) `RulesEngine.validate(listing: CanonicalListing, marketplace: str, category: str) -> ErrorReport` as a PURE function — no DB session, no LLM, rules passed in as loaded data, enforced by a ruff banned-api rule making it impossible for app/services/rules_engine.py to import openai (this is how invariant 2 becomes mechanically true instead of culturally true); seed the rules table with Amazon title ≤200, Flipkart title ≤150, the banned-terms list and required category attributes for the two launch categories; expose `POST /api/v1/rules/dry-run` which persists nothing. (2) The AI seam: `PipelineDeps` as a Protocol we implement (`retrieve_brand_chunks`, `load_rules`, `load_category_template`, `validate_rules`, `validate_images`, `record_agent_run`, `emit_node_event`, `get_model_credentials`) plus `run_listing_pipeline(state: AgentState, deps: PipelineDeps) -> GenerationResult` as the ONE entry point our generation worker calls in-process — the AI package imports no SQLAlchemy, no FastAPI, opens no session, reads no os.environ and never sees marketplace_accounts, enforced by an import-linter contract forbidding `packages.pipeline -> apps.api`. Ship a FakeAdapter and a stub pipeline so the seam is exercised before the AI team writes a node. State in writing to the AI team that the graph has NO publish node and NO approval node: it terminates at quality_check returning NEEDS_APPROVAL or BLOCKED, and publish is a separate ARQ job enqueued only by POST /api/v1/listings/publish after asserting APPROVED — spec [16]'s own node table lists `await_approval` and `publish` as graph nodes, contradicting invariant 1, and [16] is the document they will build from. (3) Read-only SQLAdmin at `/internal/admin` over tenants, users, products, listings, listing_versions, validation_runs, agent_runs, job_runs, images, rules, marketplace_accounts, audit_log with can_create/can_edit/can_delete all False and `token_enc` + `hashed_password` excluded from both column_list and column_details_list (rendering token_enc as hex in a browser tab is how encrypted credentials end up in a Slack screenshot) — plus four hand-written Jinja ops pages under /internal/ops: /queue (depth + oldest job age + DLQ size), /cost (daily spend, spend per tenant, cost per successful listing, from SQL views not log metrics — money comes from Postgres, which is exact and free to query), /funnel (listings by status, last 24h), /dlq (with a requeue button enabled only when ENV != production). Deployed as its own Cloud Run service `api-admin` with `--no-allow-unauthenticated` and roles/run.invoker on a `eng-admin@` Google Group, reached via `gcloud run services proxy` — it deliberately bypasses TenantScopedRepository, which is exactly why it is read-only and IAM-gated, and why it must NOT reuse the tenant `admin` role (that role is an admin INSIDE one tenant, not platform staff). Budget ~1 day; building our own React admin would be 3–5 days duplicating the FE team. (4) ONBOARDING.md: the 10 invariants, a 10-minute architecture read, the repo tour, how to run everything, how to get GCP access, the ADR process and the ticket flow — written incrementally across Days 2–9, not in a panic on Day 10.


**Unblocks:** Feature tickets. After this step the AI team can run their pipeline against real ports with fakes, the FE team has a real API plus a mock plus generated types, and we can see with our own eyes whether import → validate works before any dashboard exists.


**Done when:** The rule fixture suite passes 100% in CI and flags a 221-char Amazon title and a banned term with the exact frozen ErrorReport JSON; `run_listing_pipeline` executes end-to-end against the stub pipeline and FakeAdapter on a laptop with no GCP credentials and no LLM key; /internal/admin renders a seeded listing's raw content_json and its ErrorReport, and a CI test asserts it returns 404 when ENV=production and ADMIN_ENABLED is unset; ONBOARDING.md gets a new reader from clone to a merged dummy PR without asking a question.


---

## Can start immediately, in parallel, with no dependencies

- Send ONE client email today containing every blocking ask in a single message — Amazon SP-API (both paths, roles scoped to Product Listing/Inventory/Catalog only), Flipkart partner application + a direct note to their account manager + the written sandbox question, Professional seller account confirmation, KYC pack, privacy policy URL, GCP org + billing, GitHub Team seats, OpenAI org + $100 prepay, domain nomination, brand voice guide + glossary + past listings, real-format sample CSV, the two launch categories, and the two named UAT humans. One message, numbered, with dates. Chasing these one at a time across a week is how the Day-29 adapter work slips.
- Write ADR-0001 (GCP supersedes the AWS text in spec §15/§17/§18/§20) and ADR-0002 (uv + Python 3.11 + one monorepo) and mark those spec files SUPERSEDED in the repo — ten minutes each, and it stops someone provisioning RDS in week 4 or quoting spec 187 at you.
- Scaffold the monorepo tree, Makefile, docker-compose.yml, ruff/black/mypy/pytest config and .pre-commit-config.yaml. Needs no GCP, no client, no decisions — pure local work that can start this minute.
- Write the packages/schemas Pydantic models. No DB, no GCP, no credentials needed. This is the item that unblocks two other teams, so it gets the best hours of today and tomorrow, not the leftovers.
- Write the DB contract note and send it to the AI and FE teams before the migrations exist: final column names (tenant_id everywhere), the listings.status / pipeline stage split, the agent_runs write contract (they set model/tokens/cost via record_agent_run on EVERY call including embeddings, and still write tenant_id on embedding runs which have no product_id, or brand-voice cost becomes untraceable to a customer), the brand_chunks read contract (repository method only, top-k=8, 1536 dims, 2000-dim HNSW ceiling), and the job_runs polling contract.
- Draft CODEOWNERS and the branch-protection rule set, and create the four GitHub teams (@backend, @ai-eng, @frontend, @devops-qa) so they exist well before Day-11 onboarding. packages/schemas requires review from all three teams — this is the cheapest possible protection against a three-team contract break, and it is a five-minute file.
- Write apps/api/.env.example and the pydantic-settings Settings class that refuses to boot on a missing required var. The spec's .env.example is 13 vars and omits CORS, rate limits, token TTLs, KMS, OAuth redirect URIs, OAuth state TTL, pool sizes, NEXTAUTH_*, the storage backend switch and the per-provider LLM keys.
- Build the deterministic Rules Engine as a pure function with its fixture test suite. It needs no database, no LLM and no client data — seed it with the Amazon ≤200 / Flipkart ≤150 title caps and a banned-terms list, and swap in the real category rules when the client names the two launch categories.
- Write the golden fixtures (canonical_listing.valid.json, canonical_listing.minimal.json, error_report.field_too_long.json, error_report.passed.json, image_report.background_off.json, generation_result.partial.json, listing_result.partial_publish.json, import_report.json) and commit them. Generated from the Pydantic models so they cannot drift. The FE team uses them for MSW mocks, the AI team for golden-output tests, we use them for contract tests.
- Add gitleaks and detect-private-key to pre-commit AND as a required CI check scanning the PR range. Twenty minutes today; on Day 41 the same problem is a credential rotation across three teams plus a git history rewrite.
- Start the account register: every account with its owner, root-credential holder, billing status and what gets revoked at project end. Ten minutes of bookkeeping while the accounts are being created; a week of archaeology if left to Day 45.
