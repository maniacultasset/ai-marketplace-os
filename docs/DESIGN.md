# Backend design conventions

Purpose: one front-door summary of the conventions every backend change follows.

Read with: `docs/architecture/BACKEND-ARCHITECTURE.md` (§4–§9, §11.4), `docs/architecture/DATABASE.md` (§1–§8), `docs/canon/CANON.md` ("Amendments by ADR" first), `docs/canon/CANON-ADDENDUM.md`, `docs/canon/ENV.md`, `docs/adr/README.md`, `docs/review/TEAM-QUESTIONS.md`.

**This file is a summary, not a source.** The documents above are the single source of truth. Where they disagree with this file, they win (precedence: `docs/README.md`).

**Tags** (`BACKEND-ARCHITECTURE.md` §1.4): **[Site]** is the client spec (`L<line>` in `docs/source/system-arch-llms-full.txt`). **[Decided]** is an accepted ADR, a Canon row or a recorded user decision. **[Proposed]** awaits approval. **Open:** names the T-row or decision number that tracks an unresolved point.

**Status:** S1 merged 2026-09-30 (PR #3). Only the health probes (with a DB readiness check), settings, the DB session, the empty `/api/v1` mount, `0000_bootstrap.sql`, the compose Postgres and CI exist; the rest is unbuilt design. Source: `docs/STATE.md` "Where we are"; `apps/api/app/main.py:40-41`; `apps/api/app/db/session.py`; `apps/api/app/services/health.py`; `scripts/db/0000_bootstrap.sql`; `docker-compose.yml`.

## 1. Layers

- A request goes router → service → repository [Site L12903]:
  - routers do HTTP only (models, identity, roles, error mapping) and contain no SQL;
  - services own business rules, the transaction and audit, and workers call the same services;
  - repositories hold all SQL and extend `TenantScopedRepository`.
- `.importlinter` enforces these layers today.
- Only `app/core/config.py` reads the environment (`APP_ENV`).
- Domain code never imports a cloud SDK; it goes through `StorageAdapter` and the KMS `KeyWrapper` [Decided ADR-0001].

Source: `docs/architecture/BACKEND-ARCHITECTURE.md:1191` (§4.3), `:1217` (§4.4); `.importlinter`.

## 2. API design

- **Versioning:** business routes live under `/api/v1`. The only unversioned routes are `/healthz /readyz /openapi.json /docs /internal/admin` [Decided].
  - Open: T11 (the site says the MVP has no versioning, L11545).
  - `apps/api/tests/unit/test_route_prefix.py` enforces the prefix.
  - Production serves no `/docs` or `/openapi.json` (`apps/api/app/main.py:27-32`).
- **Naming:** collection nouns are plural. A nested path is resource then verb (`/marketplaces/{marketplace}/callback`). Actions are `POST` sub-paths (`/listings/approve`).
- **Auth** [Decided; Open: T7]:
  - FastAPI issues an HS256 JWT: access 30 min, refresh 14 d with rotation.
  - The tenant comes only from `tid`, never from a path, query or body. Workers take it from `job_runs`, and the OAuth callback from `oauth_states`.
  - Approve needs reviewer or owner; publish needs owner or operator.
- **Errors:** every failure is one `ErrorReport` [Site L11633-11672]: `status, error_code, retryable, attempts_used, max_attempts, errors[{field, error_code, message, value_submitted, suggestion, source, retryable, marketplace}]`.
  - `source` is one of `schema|rules_engine|vision_model|ai_model|amazon_adapter|flipkart_adapter|system`.
  - Codes come only from `mkos_schemas/codes.py`.
  - [Proposed] Every non-2xx is `{"error": {type, message, status, correlation_id, details, error_report}}`.
- **Pagination** [Proposed]: `{"data", "page": {next_cursor, has_more, limit}}`, an opaque keyset over `(created_at, id)`, no OFFSET. Open: page sizes are not specified (no T-row).
- **Idempotency** [Decided invariant 10]: `idempotency_keys`, PK `(tenant_id, key)`.
  - [Proposed] `Idempotency-Key` is required on publish and bulk import.
  - The same body replays the stored response; a different body gets 409. Keys expire after 24 h.
- **Async jobs:** `202 JobAccepted {job_id, poll_url}`. Clients poll `GET /api/v1/jobs/{job_id}`, which reads `job_runs`, never the queue.
- **Rate limits** [Decided ADR-0005]: 120/min per tenant, 60/min per IP (unauthenticated), 5/min on login, then 429. Counters live in an UNLOGGED Postgres table. The body cap is 10 MiB.

Source: `docs/canon/CANON.md:33-36`, `:57`, `:60`, `:73`, `:83`; `docs/architecture/BACKEND-ARCHITECTURE.md:1286-1452` (§5), `:1977` (§9.2), `:2175` (§9.6); `docs/architecture/DATABASE.md:978`; `docs/canon/ENV.md:13-20`.

## 3. Data design

- **Ids and time:** `id uuid DEFAULT gen_random_uuid()` and `created_at timestamptz DEFAULT now()`. Mutable tables add `updated_at` by trigger. Open: UUIDv7 (DATABASE.md decision 14).
- **Tenancy:** the column is always `tenant_id`, never `brand_id`.
  - Every tenant table has `UNIQUE (tenant_id, id)`, and child rows reference their parent by `(tenant_id, parent_id)`.
  - Actor columns reference `tenant_memberships`. Open: T23.
  - Job references from longer-lived tables are soft (no FK).
- **Isolation** runs in four layers: the repository filter, the ORM guard, RLS, and composite FKs.
  - RLS is ENABLE + FORCE on every tenant table [Decided, Canon `0005_rls`].
  - [Proposed] the tenant is set with `set_config('app.tenant_id', …, true)` per transaction, and no role has BYPASSRLS.
  - The one sanctioned bypass is `platform_scope()`.
- **Vocabularies:** `TEXT + CHECK`, never ENUM. `marketplace` is limited to `amazon|flipkart` [Proposed]. Open: T9.
- **Soft delete:** `deleted_at` on `products` and `images` [Decided], and on a few more tables [Proposed]. Unique indexes are partial, `WHERE deleted_at IS NULL`.
- **JSONB** holds only typed documents: the ErrorReport, `content_json` (a `CanonicalListing`), and `payload_json` (ids only, ≤ 64 KiB). Blobs go to object storage.
- **Vectors:** `vector(1536)`, HNSW cosine (`m=16, ef_construction=64`), pgvector ≥ 0.8.2 [Decided ADR-0003].
  - Queries filter on `tenant_id` and `embedding_model`, top-k 8.
  - Open: T66 (exact search in year 1).
- **Transactions:** none stays open across an LLM, KMS, storage or marketplace call.
- **Migrations:**
  - Revision ids are claimed in the Canon ledger first.
  - `0000_bootstrap.sql` is raw SQL; Alembic runs as `migrator` from a Cloud Run job before each deploy.
  - The re-planned ledger is in DATABASE.md §8.2 [Proposed].
- **Expand/contract** [Decided 2026-09-27 #5]: rollback means redeploying the previous tag. Release N must run with N−1's code, so a single release never renames, changes a type or drops anything.

Source: `docs/architecture/DATABASE.md:20-27`, `:61`, `:100`, `:1057`, `:1756`, `:1799`; `docs/canon/CANON.md:47`, `:52`, `:75-76`.

## 4. Jobs, retries, dead letters

- `job_runs.status` is one of `queued|running|succeeded|partial|failed|cancelled|dead_letter`. A timeout is recorded as `JOB_TIMEOUT` on a `failed` row.
- **Queue** [Decided ADR-0004; Open: the spike]: procrastinate on Postgres, with queues `default` and `publish`. Jobs are enqueued in the same transaction, and the payload is only `job_run_id`.
- **Timeouts:** 300 s for generation, 600 s for publish, 1800 s for import.
- **Retries:** transport errors, 5xx and 429 retry with backoff; `max_attempts` defaults to 3.
  - Generation uses `max_attempts=1`, because AI self-correction is capped at 3 inside the graph, then the listing is `blocked` (invariant 9).
  - Open: DATABASE.md §11.1 #11 (what the cap counts).
- **Dead letters:** exhausted retries → `dead_letter`. A requeue inserts a new row with `parent_job_id`. Open: VISION-ALIGNMENT Q18 (who may requeue).
- **Publish retry** resends only the failed fields [Site L12033]. At most 3 submissions per approved version [Proposed]. Open: T19.

Source: `docs/architecture/BACKEND-ARCHITECTURE.md:1670-1864` (§8); `docs/architecture/DATABASE.md:928`, `:1465`; `docs/canon/CANON.md:51`, `:61`; `docs/adr/0004-postgres-job-queue.md:25-47`.

## 5. Listing lifecycle

- `listings.status` has 9 values: `created|generating|validated|needs_review|approved|publishing|published|blocked|failed` [Decided]. Open: T12 (the BRD's 14 statuses become a display only).
- **Path:** `created → generating → validated|needs_review → approved → publishing → published`. The side exits are `blocked` and `failed`. An edit voids the approval.
- **Published vs live:** `published` means the marketplace accepted the listing. Open: T13 (when it counts as live).
- **Maker-checker** [Site L6620]: whoever generated or edited a version cannot approve it. The API and a DB trigger both check this. Open: T10, T15.

Source: `docs/canon/CANON.md:53-54`; `docs/architecture/DATABASE.md:1373` (§6.1), `:1565` (§7.1); `docs/architecture/BACKEND-ARCHITECTURE.md:2038` (§9.3).

## 6. Marketplace adapters

- Only adapters call Amazon or Flipkart [Site L10533]. The canonical listing has no marketplace-specific fields.
- `MarketplaceAdapter` methods: `get_listing`, `create_listing`, `update_listing`, `publish_listing`, `get_performance` [Site L10519-10524]. Each returns a per-field `ListingResult`, never a boolean.
- Adapters own auth refresh, rate limiting, idempotency and error mapping [Site L10531]. Only adapters decrypt seller tokens.
- A `FakeAdapter` stands in for tests [Decided].

Source: `docs/architecture/BACKEND-ARCHITECTURE.md:1204`, `:2080`, `:3106`.

## 7. The AI seam

- One call [Decided]: `async def run_listing_pipeline(ctx: GenerationContext, deps: PipelineDeps) -> GenerationResult`.
- Context is pushed, not pulled. Open: when it is assembled (BACKEND-ARCHITECTURE.md §13.5).
- There are exactly five ports: `retrieve_brand_chunks`, `validate_rules`, `validate_images`, `get_model_credentials`, `emit_node_event`.
- The graph has 12 transient stages and no approval or publish node. Only the backend writes `agent_runs`, one row per model call.
- **Home** [Decided ADR-0012; Open: T104]: the pipeline lives in the AI team's repo, pinned by tag. This repo keeps a stub.
- **Changes:** a Canon PR plus a schemas bump, approved by three leads. MAJOR changes land only on Day 14, 28 or 42.

Source: `docs/canon/CANON.md:100-131`; `docs/architecture/BACKEND-ARCHITECTURE.md:1454-1635` (§6); `docs/adr/0012-backend-only-repository.md:25-46`.

## 8. Observability

- **Request ids:** every request gets a UUID `request_id` that flows into jobs and runs [Site L6650]. `job_runs` also carries a fresh `correlation_id`.
- **Logs:** structured JSON; sensitive data is never logged [Site L12643].
- **Traces and errors:** OpenTelemetry to Cloud Trace (sampling 0.1); Sentry with `send_default_pii=False`.
- **Alerts:** queue-latency and budget alerts go to `ALERT_SLACK_WEBHOOK`. Open: where the metrics live (no T-row).

Source: `docs/architecture/BACKEND-ARCHITECTURE.md:2697` (§11.4); `docs/canon/ENV.md:120-126`.

## 9. Changing a convention

- **Canon:** a Canon row changes only through a PR to `CANON.md` (Tech Lead plus the owning team).
- **ADRs:** a number is claimed in `docs/adr/README.md` before the file is written.
- **Generated contracts:** regenerate with `make -C packages/schemas all`; never hand-edit.
- **Error codes:** a new one needs the Tech Lead.

Source: `docs/canon/CANON.md:3`, `:43-45`; `docs/adr/README.md:3`; `docs/architecture/BACKEND-ARCHITECTURE.md:1398`.
