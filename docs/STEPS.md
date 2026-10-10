# STEPS: the v1 build steps and their "Done when" lines

**Status: approved (user via chat, 2026-10-04)** (STATE P17; EXECUTION-PLAN P0-A7). Approval
includes deploys moving into S4, the S14a/S14b split, the "done (local)" status and the moved
criteria. The open questions at the end are answered inside each step's plan.

**Purpose.** The single tracked list of v1 build steps, S2–S26, with testable acceptance criteria.
Each step's `PLAN.md` copies its "Done when" lines as acceptance criteria; a step is done only when
every line is proven with fresh output.

**Supersedes.** Once approved, this file replaces the scope paragraphs and "Done when" lines of
`docs/drafts/BUILD-SEQUENCE.md` S2–S14 (BUILD-SEQUENCE.md:23-241) and gives S15–S26 their first
"Done when" lines. S1 is merged (PR #3, follow-ups PR #6).

**Sources, highest first** (`docs/README.md` "When sources disagree"): the user's latest instruction
and `docs/STATE.md`; accepted ADRs 0001–0005, 0012, 0013; proposed ADRs 0006–0011 built as written
(STATE P7); `docs/canon/CANON.md` with "Amendments by ADR" (CANON.md:10-27) and `CANON-ADDENDUM.md`
(A§n); `docs/architecture/DATABASE.md` rev 3, the accepted schema source (STATE open decision 10);
`docs/architecture/BACKEND-ARCHITECTURE.md`; `docs/EXECUTION-PLAN.md` §6.2–§6.6, §10; the drafts.

**Corrections applied to every BUILD-SEQUENCE step:**
- Python 3.11 → `==3.13.*` (ADR-0002); pg16 → PostgreSQL 17 + pgvector ≥ 0.8.2 (ADR-0003); Redis +
  ARQ → procrastinate on Postgres, queues `default`/`publish`, transactional enqueue, no sweeper, no
  Redis (ADR-0004); asyncpg → psycopg 3 (ADR-0004 §3).
- Monorepo with `apps/web` and `packages/pipeline` → backend-only repo; the AI pipeline lives in the AI
  team's repo, pinned by tag, with a stub in our CI (ADR-0012 §1, §3; T103, T104); `@mkos/schemas`
  ships on GitHub Packages (ADR-0012 §2).
- "Eight contracts" → 17 schema modules (A§21); 14 PipelineStage names → 12 (A§18); 8 PipelineDeps
  ports → 5 (CANON.md:119); AI-team `record_agent_run` → `GenerationResult.agent_runs[]` (CANON.md:113).
- 12 required checks → 10 (ADR-0012 §4; SUPERSEDED.md:48 still says 12): `api-lint`, `api-typecheck`,
  `api-tests`, `tenant-isolation`, `lint-imports`, `bootstrap`, `contracts`, `schemas-drift`,
  `commit-lint`, `gitleaks` (EXECUTION-PLAN §4.4).
- Ledger: `0000_bootstrap.sql` is raw SQL (CANON.md:92); then `0001`, `0002`, `0002b`, `0003`,
  `0003b_procrastinate`, `0004_integrity`, `0005_rls` with DATABASE.md §8.2's 9 / 16 / 14 tables. RLS
  lands in S10 (EXECUTION-PLAN.md:600-601).
- Criteria that needed a later step's artifacts move to that step, marked "(moved from Sn)"
  (KNOWN-ISSUES.md:38).

**Tags on criteria.**
- **[staging]** needs billing, the domain or a deployed environment. A step whose other lines pass is
  **done (local)**; its [staging] lines close later (STATE "Start here" 2.9; PHASES.md:91-92).
- **[external]** needs a third party: marketplace credentials, the AI team's drop, the client.
- **[1.1 via Cn]** moved out of the 4 Nov launch by cut Cn (EXECUTION-PLAN.md:697-702). Planning basis
  (STATE "Decisions of 2026-10-04"): Amazon-first on 4 Nov with C1–C10, Flipkart behind a flag; the
  client still confirms (T16, VISION Q25).
- **[user-go]** an outward action only the user takes: a tag, a push, a publish, `tofu plan/apply`.

**Rules for every step:** tests first, and every guard proven able to fail (`scripts/ci/prove_gates.sh`);
all 10 checks green with fresh output, and the ADR-0013 §4 reductions the step owns removed; no network
in tests (pyproject.toml:81), stand-ins only (`docs/RULES.md` §8); migration ids, Canon rows, new
`ErrorCode`s and contract changes get the user's OK before code (`docs/RULES.md` §6-§7, §11); the PR
updates `docs/PHASES.md` (`docs/RULES.md` §12). Test names (`test_*`) are proposed patterns the plan
fixes; proof runs as `make <check>` or `uv run pytest -k <pattern>` with the compose stack up.

## Order and dependencies

Order follows STATE "Start here" 2, single track until P6 is accepted. Days are EXECUTION-PLAN
targets (EXECUTION-PLAN.md:605-624, :632-643) and slip with billing (EXECUTION-PLAN.md:822-829).

| Step | Title | Depends on | Track | EP days |
|---|---|---|---|---|
| S2 | `mkos-schemas` v1.0.0 (17 modules) | S1; spike for `jobs.py` names; T1, T61 | B | D13–14 |
| S3 | `/api/v1` OpenAPI stubs, TS package, mock | S2 | B | D15–16 |
| S5 | Alembic, bootstrap roles, migrate wrapper | S1 | A | D13 |
| S6 | `0001_core_tenancy` | S5 | A | D14 |
| S7 | `0002` + `0002b` HNSW | S6 | A | D15–17 |
| S8 | `0003` + `0003b_procrastinate` | S7, spike passed | A | D17–18 |
| S4 | Staging OpenTofu + `deploy-staging.yml` | S1, EP decision 15 | B | D17–18 |
| S9 | Repository guard, isolation gate, seed | S7, S8 | A | D19–20 |
| S11 | Observability spine | S8 | B | D19–20 |
| S10 | Auth, RBAC, `0004_integrity`, `0005_rls` | S9, T7 | A | D21–23 |
| S14a | Rules Engine (pure) + fixtures | S2, S7 | B | D21–22 |
| S12 | Walking skeleton | S10, S11 (+S4, DNS for [staging]) | A | D24–25 |
| S17a | Pillow checks library | S2 | B | D26 |
| S13 | Job spine + CSV import | S8, S11, S12 | A | D26–27 |
| S14b | AI seam, `/rules/dry-run`, `/internal/admin`, ONBOARDING | S3, S9, S10, S13, S14a | A | D28–29 |
| S15 | Marketplace accounts, OAuth, KMS | S10, S13 (+S4 for [staging]) | B | D28–29 |
| S16 | Brand onboarding + voice vectors | S10, S13 | B | D30–31 |
| S18 | Generation through the seam | S14b; S16 before final gates | A | D30–32 |
| S19 | Review + approval | S18 | A | D33–34 |
| S20 | Adapter framework + Amazon | S14b, S15 | B | D32–35 |
| S21 | Publish, per-field retry, status sync | S19; S20 before final gates | A | D35–37 |
| S17b | Image pipeline (minimal) | S13, S17a | B | D36–37 |
| S22 | Flipkart adapter behind a flag | S22a (adapter, mapping, flag, connect + preview): S15, S20; S22b (publish wiring): S21 | B | D40–41 or later |
| S23+S24 | Analytics (4 metrics) + observability completion | S21, S11 | B | D38–39 |
| S25 | Prod OpenTofu, `deploy-prod.yml`, drills | S4, domain | B | D23–25 |
| S26 | Launch hardening | all above | A (+B) | D38–41 |

EXECUTION-PLAN §6.2 starts S13 after S12 over TLS; here S13 needs only S12's local half, so it can
start before billing. S14b gains S14a as a dependency because `/rules/dry-run` calls the engine.

---

## S2 · `mkos-schemas` v1.0.0: the cross-team contract

- **Depends on:** S1. `jobs.py` names (`queue_job_id`, timeouts) assume the ADR-0004 spike passes; T1
  (Python 3.13) and T61 (nested CanonicalListing) run on their defaults.
- **Scope:**
  - The 17 modules of A§21, pydantic the only dependency (CANON.md:113); `StrictModel` for inbound
    bodies, `WireModel` (`extra="ignore"`) for cross-team payloads (BUILD-SEQUENCE.md:31; T100).
  - Frozen vocabularies: `ListingStatus` (9, CANON.md:53), `PipelineStage` (12) and `JobState` (7) as
    A§18, `ErrorSource` (7, A§19), `ImageSlot` (CANON.md:58); the 5 codes and 2 paths of A§20.
  - Seam types: `PipelineDeps` (5 ports), `GenerationContext`, `GenerationResult`, `BrandChunkDTO`,
    `RuleSet`/`Rule`/`RuleDef` (A§16), `CategoryTemplate` (+ `variation_themes`, ADR-0007),
    `ModelCredentials` (CANON.md:117-123); models for `ImportReport` and `JWTClaims` (KNOWN-ISSUES.md:28).
  - Golden fixtures generated from the models; `export.py` writes JSON Schema to `jsonschema/`.
- **Done when:**
  1. In a bare venv, `pip install -e packages/schemas && python -c 'from mkos_schemas.listing import
     CanonicalListing'` works. Proof: `make contracts`.
  2. Every module with models has a golden fixture that round-trips `model_validate → model_dump(by_alias=True)`
     unchanged, and a test fails if a model has none. Proof: `test_golden_roundtrip_*`.
  3. `model_json_schema(mode="serialization")` and `ImageReport.model_dump()` both emit `pass`, never
     `pass_` (KNOWN-ISSUES.md:53). Proof: `test_image_report_pass_alias`.
  4. Each vocabulary equals its Canon set exactly; A§18's deleted names are absent. Proof: `test_enum_*_matches_canon`.
  5. `PipelineDeps` has exactly the ports of CANON.md:119; `load_rules`, `load_category_template`,
     `validate_schema`, `record_agent_run` are members nowhere (A§17). Proof: `test_pipeline_deps_ports`.
  6. `codes.py` has the 4 import codes and `JOB_TIMEOUT` beside `TIMEOUT`, and only `FIELD_TOO_LONG` for
     over-long fields (A§20; T83); `paths.py` accepts `file.header`, `file.row` and attribute paths with
     digits (KNOWN-ISSUES.md:53). Proof: `test_codes_*`, `test_paths_*`.
  7. An over-long `value_submitted` is truncated, not rejected; the `RuleDef` discriminator is required
     in JSON Schema (KNOWN-ISSUES.md:53). Proof: `test_error_item_truncates`, `test_ruledef_discriminator`.
  8. The JobEnvelope field is `status`, not `state` (SUPERSEDED.md:27). Proof: `test_job_envelope_fields`.
  9. `make -C packages/schemas all` regenerates with no diff. Proof: `make schemas-drift`.
  10. [user-go] Tag `schemas-v1.0.0` with a changelog entry and a notice to the AI and FE teams
      (`docs/RULES.md` §11; STATE P8).
- **Trace:** CANON.md:42, :50-58, :100-130; A§16-§21; KNOWN-ISSUES.md:15, :25-29, :53; ADR-0007; T1, T12,
  T21, T60, T61, T83, T84, T86, T89, T100 (EXECUTION-PLAN.md:705-707).
  **Stand-in:** none. **Waits:** the teams' notice (P8). **Open:** OQ1–OQ4.

## S3 · `/api/v1` surface as OpenAPI, the TypeScript package, a mock

- **Depends on:** S2.
- **Scope:**
  - Every planned endpoint under `/api/v1` as a 501 stub with its real `response_model`; only
    `/healthz`, `/readyz`, `/openapi.json`, `/docs`, `/internal/admin` are unversioned (CANON.md:32-33).
    The list is BACKEND-ARCHITECTURE §5.2 (BACKEND-ARCHITECTURE.md:1321-1356).
  - Envelopes everywhere: errors `{"error": {type, message, status, correlation_id, details,
    error_report}}`; lists `{"data": [...], "page": {...}}` with a `(created_at, id)` keyset cursor
    (BACKEND-ARCHITECTURE.md:1400-1411).
  - `packages/schemas/jsonschema/openapi.json` and `packages/schemas/ts/src/{api,contracts,index}` from
    `make -C packages/schemas all` (CANON.md:43-45); `@mkos/schemas` on GitHub Packages; a local mock;
    `contracts` gains the oasdiff check (CANON.md:138; ADR-0013 §4).
- **Done when:**
  1. The OpenAPI path set equals the plan's frozen list; no business path lacks `/api/v1`. Proof:
     `test_openapi_paths_frozen`, `test_route_prefix` (apps/api/tests/unit/test_route_prefix.py).
  2. Every non-2xx response, request-validation errors included, matches the error envelope with
     `source="system"` (A§19). Proof: `test_error_envelope_*`.
  3. `POST /api/v1/listings/validate` is declared 200 with `passed: false` on a rules failure, never 422
     (BUILD-SEQUENCE.md:48). Proof: `test_validate_contract_200`.
  4. A hand edit to generated output turns `schemas-drift` red; removing a response field turns
     `contracts` red. Proof: `prove_gates.sh` cases.
  5. The mock answers `GET /api/v1/products` with fixture data on `127.0.0.1`. Proof: a smoke test.
  6. [user-go] `@mkos/schemas` published at the version matching the schemas tag (ADR-0012 §2).
- **Trace:** CANON.md:32-37, :43-45, :60, :138-139; SUPERSEDED.md:46-47; BACKEND-ARCHITECTURE §5; spec
  L11299, L11545 (T11). **Stand-in:** the mock serves the FE team (PHASES.md:62). **Waits:** FE adoption
  (P0-U9); the draft's "FE confirms in writing" becomes a notice. **Open:** OQ5, OQ6.

## S5 · Alembic, bootstrap roles, the migrate wrapper

- **Depends on:** S1.
- **Scope:**
  - Alembic on psycopg 3: `naming_convention`, `compare_type`, `compare_server_default`,
    `transaction_per_migration`, a `render_item` hook for `Vector(1536)` (BUILD-SEQUENCE.md:82).
  - `scripts/db/0000_bootstrap.sql` (exists since S1) extended per DATABASE.md §8.1: `app_ro`,
    `app_platform_ro`, the Cloud SQL self-grant, EXECUTE revoked from PUBLIC, role timeouts, passwords
    only via psql variables; no BYPASSRLS anywhere (DATABASE.md:1743-1752; KNOWN-ISSUES.md:32).
  - Engine: pool 5/5, pre-ping, recycle 1800, timeout 10, statement timeout via libpq `options` (ENV.md:36-41).
  - `python -m app.cli db migrate`: advisory lock, `lock_timeout` 5 s, refuses a DB newer than the image
    (EXECUTION-PLAN.md:300-303). `api-tests` and `bootstrap` regain Alembic and squawk (ADR-0013 §4).
- **Done when:**
  1. `upgrade head → downgrade base → upgrade head` is clean on compose PostgreSQL 17. Proof: `make api-tests`.
  2. `alembic check` fails when a model drifts from the head. Proof: `prove_gates.sh` case.
  3. Autogenerate on a `Vector(1536)` model writes the import and the migration runs. Proof:
     `test_autogenerate_vector_column`.
  4. Running `0000_bootstrap.sql` twice is a no-op; `app_rw`, `app_ro`, `app_platform_ro` exist, none
     is SUPERUSER or BYPASSRLS, `app_platform_ro` is NOLOGIN. Proof: `test_bootstrap_sql_*`
     (apps/api/tests/integration/test_bootstrap_sql.py).
  5. `app.cli db migrate` refuses a database ahead of the code. Proof: `test_migrate_refuses_newer_db`.
  6. squawk runs on the rendered SQL, report-only until the first prod tag (EXECUTION-PLAN.md:303-305).
  7. [staging] [user-go] `0000` and `upgrade head` on `aimos-db` through the proxy (STATE P9).
- **Trace:** CANON.md:75-76, :92; A§1-§3, §7; SUPERSEDED.md:57-58; DATABASE.md §8.1-§8.3;
  KNOWN-ISSUES.md:32; EP decision 16; T58, T59. **Stand-in:** compose Postgres. **Waits:** billing (item 7).

## S6 · Migration `0001_core_tenancy`

- **Depends on:** S5; ADR-0006/0007/0008 built as written (STATE P7).
- **Scope:** the 9 tables of DATABASE.md:1763: `tenants` (+ `status`, `plan`, `trial_ends_at`,
  `auto_publish` with `CHECK (auto_publish = false)`), global `users` unique on `lower(email)`,
  `tenant_memberships`, `invitations`, `products` (+ family and fact columns, `deleted_at`),
  `brand_profiles`, `business_parties`, `product_colourways`, `variant_matrix_cells`; functions
  `set_updated_at()`, GUC helpers, `fact_map_valid()`, `gtin_valid()`; anchors `UNIQUE (tenant_id, id)`.
  `audit()` **moves to S10** (`0004_integrity`, after `audit_log` in 0002; DATABASE.md:1765, :1770).
- **Done when:**
  1. A duplicate SKU in one tenant is rejected; the same SKU in a second tenant is accepted. Proof:
     `test_products_sku_unique_per_tenant`.
  2. `A@x.test` and `a@x.test` cannot both exist (ADR-0006 §1; fixes KNOWN-ISSUES.md:16). Proof: `test_users_email_unique_ci`.
  3. One membership per (tenant, user), roles only `owner|operator|reviewer|admin`. Proof: `test_memberships_*`.
  4. A child whose parent is in another tenant fails at the composite FK; a product cannot be its own
     parent; only parents carry `variation_theme`; identical stored variant keys under one parent
     collide. Proof: `test_product_family_*`. (The `M/Navy` vs `m/navy` case-fold collision, ADR-0007
     §4, **moves to S10**: the 0004 variant guard writes the key; DATABASE.md:1765.)
  5. Setting `tenants.auto_publish = true` fails (ADR-0008 §2). Proof: `test_auto_publish_locked`.
  6. `gtin_valid()` checks digits and the EAN-13/GTIN-14 case (DATABASE.md:1795). Proof: `test_gtin_valid`.
  7. Upgrade/downgrade of `0001` clean. Proof: `make api-tests`.
- **Trace:** CANON.md:27, :47-48, :93; ADR-0006–0008; DATABASE.md §5.1, §5.2, §5.6; T15, T23, T32; spec
  L5885-5892. **Stand-in:** none. **Waits:** none. **Open:** OQ7.

## S7 · Migration `0002_listing_pipeline` + `0002b_brand_chunks_hnsw`

- **Depends on:** S6.
- **Scope:**
  - The 16 tables of DATABASE.md:1764: the 11 Canon tables (A§25) plus `review_decisions`, `batches`,
    `listing_items`, `human_tasks`, `listing_issues`.
  - Corrections: `audit_log` unpartitioned with nullable `tenant_id` and an `actor_type` CHECK
    (CANON.md:81; KNOWN-ISSUES.md:33); `rules` unique as the COALESCE expression index including rule
    type and attribute (A§4; KNOWN-ISSUES.md:23); `validation_runs.source` over 7 values (A§19);
    `cost_usd NUMERIC(12,6)`; `images.slot` CHECK with no default.
  - `0002b` alone: HNSW (m=16, ef_construction=64) plus the partial index, AUTOCOMMIT, asserting
    pgvector ≥ 0.8.2 (CANON.md:95; ADR-0003).
- **Done when:**
  1. A catalog test asserts every `tenant_id` column is NOT NULL (except `audit_log`'s documented case),
     indexed and anchored. Proof: `test_tenant_columns_catalog`.
  2. A `listing_version` with another tenant's `tenant_id` than its listing fails the composite FK.
     Proof: `test_listing_version_tenant_fk`.
  3. `listings.status` accepts exactly the 9 values; `validation_runs.source` rejects `rules` and `api`;
     `images.slot` rejects `swatch`. Proof: `test_vocab_checks_*`.
  4. Two rules differing only in rule type for one (marketplace, category, field, version) both insert.
     Proof: `test_rules_unique_includes_type`.
  5. `0002b` fails clearly on pgvector < 0.8.2; on 0.8.5, `EXPLAIN` of a top-8 cosine query uses
     `brand_chunks_embedding_idx`. Proof: `test_hnsw_*`.
  6. Upgrade/downgrade clean through `0002b`. Proof: `make api-tests`.
- **Trace:** CANON.md:53-58, :81, :94-95; A§4, §19, §25; SUPERSEDED.md:60-62; KNOWN-ISSUES.md:23, :33-34;
  DATABASE.md §5.3, §5.5, §5.7; T12, T50, T60, T66, T89. **Stand-in:** none. **Waits:** none. **Open:** OQ8.

## S8 · Migration `0003_ops_tables` + `0003b_procrastinate`

- **Depends on:** S7; the ADR-0004 spike passed on its local half (STATE P9).
- **Scope:**
  - The 14 tables of DATABASE.md:1766: the 6 Canon ops tables (`job_runs` with `queue_job_id`, not
    `arq_job_id`; `imports`; `import_rows`; `idempotency_keys`; `refresh_tokens`; versioned
    `category_templates`) plus `oauth_states`, UNLOGGED `rate_limit_counters`, `listing_submissions`,
    `publish_field_results`, `attribute_definitions`, `attribute_columns`, `category_attributes`,
    `marketplace_error_map`; then the `NOT VALID` + `VALIDATE` constraints.
  - `0003b`: procrastinate's shipped schema at `>=3.10,<3.11` plus grants (ADR-0004 §1).
- **Done when:**
  1. `job_runs.status` accepts exactly the 7 JobState values; `UNIQUE (tenant_id, idempotency_key)`
     holds only where a key is set. Proof: `test_job_runs_*`.
  2. `idempotency_keys` PK is `(tenant_id, key)`; every sha256 column checks `octet_length = 32`
     (DATABASE.md:27). Proof: `test_ops_constraints_*`.
  3. `rate_limit_counters` is UNLOGGED. Proof: `test_rate_limit_counters_unlogged`.
  4. procrastinate's own schema check passes against the pinned library. Proof: `test_procrastinate_schema_matches`.
  5. Upgrade/downgrade clean through `0003b`. Proof: `make api-tests`.
  6. The draft's endpoint criteria (jobs read from Postgres, the 50-row CSV, Idempotency-Key replay)
     **move to S13** (KNOWN-ISSUES.md:38).
- **Trace:** CANON.md:19-21, :49-52, :96; A§25; ADR-0004 §2-§9; DATABASE.md §5.4; T3. **Stand-in:** none.
  **Waits:** spike criterion 6 on `aimos-db` [staging] (ADR-0004:66). **Risk:** a failed spike supersedes
  ADR-0004 and returns S8/S13 to Redis + ARQ (ADR-0004:3-4); their lines here would need re-approval.

## S4 · Staging infrastructure as OpenTofu, and `deploy-staging.yml`

- **Depends on:** S1; EP decision 15 (KMS grants) recorded before the plan; STATE P10 (who runs `tofu plan`).
- **Scope:** `infra/envs/staging/` importing the user's by-hand set (EXECUTION-PLAN.md:564-579); new KMS
  `marketplace-os`/`oauth-token-key` with 90-day rotation, the 2 buckets (CSV lifecycle 30 days), worker,
  migrator, deployer, scheduler and tofu accounts, secret containers, WIF bound to `main`, audit logs,
  Scheduler stop/start, `deploy-staging.yml` (EXECUTION-PLAN.md:580-585, §4.5).
- **Done when:**
  1. `tofu fmt -check` and `tofu validate` pass for `infra/envs/staging`. Proof: the plan's make target.
  2. A test fails if any `.tf` names keyring `aimos` or key `marketplace-tokens`, grants project-level
     `secretAccessor` or creates a service-account key (KNOWN-ISSUES.md:13, :62-63; CANON.md:72). Proof:
     `test_infra_guards`.
  3. `deploy-staging.yml` follows the workflow rules (SHA pins, `permissions: {}`, no
     `pull_request_target`) and runs migrate first and the worker last. Proof: `test_workflows` (extended).
  4. [staging] [user-go] `tofu plan` shows 0 to add and 0 to destroy for the imported set; `imports.tf`
     is deleted in the same PR (EXECUTION-PLAN.md:578-579).
  5. [staging] A push to `main` deploys: migrate job, API at zero traffic, smoke, shift, worker last.
  6. [staging] A test budget threshold sends an alert (BUILD-SEQUENCE.md:70).
- **Trace:** ADR-0001, ADR-0009; CANON.md:25, :70; EXECUTION-PLAN §4.4-§4.6, §5.5, decisions 5, 15, 18.
  **Stand-in:** none (code checked locally). **Waits:** billing, OpenTofu installed; agents never run
  plan/apply (EXECUTION-PLAN §8). **Open:** OQ9.

## S9 · TenantScopedRepository, query guard, isolation gate, seed

- **Depends on:** S7, S8.
- **Scope:**
  - `TenantScoped` mixin, `TenantScopedRepository[ModelT]` as the only select factory, a
    `do_orm_execute` guard raising `CrossTenantQueryError`, and `platform_scope()` as the one bypass via
    `session.info[SCOPE_OK]` (A§6); the GUC set with `set_config('app.tenant_id', :tid, true)` (KNOWN-ISSUES.md:45).
  - `BrandChunkRepository.search(query_embedding, k=8, types=None)` with iterative scan (CANON.md:137).
  - `python -m app.cli seed --profile demo|isolation` (A§5): `acme`/`bharat`, shared `KURTA-001`,
    `KURTA-002`, `DECOR-001`, plus `ACME-900`/`BH-900` in `demo`; one membership per role per tenant;
    rules and `category_templates` (KNOWN-ISSUES.md:52); no model key or GCP credentials.
  - The real `tenant-isolation` suite replaces the S1 sentinel; `api-tests` drops its marker filter (ADR-0013 §4).
- **Done when:**
  1. `make tenant-isolation` loops every TenantScoped mapper and the vector search, and goes red when
     the repository filter is removed. Proof: `prove_gates.sh` isolation case.
  2. The suite connects as `app_rw`, not a superuser (KNOWN-ISSUES.md:30). Proof: `test_isolation_runs_as_app_rw`.
  3. A raw `select(Listing)` without scope raises `CrossTenantQueryError`. Proof: `test_guard_raises_unscoped`.
  4. `tenant_scope_checked` appears only in `app/db/guard.py` and `app/db/session.py`; `platform_scope`
     is importable only from `app.admin.*`, `app.cli`, `scripts.*`. Proof: `make lint-imports`.
  5. `demo` yields 2 tenants × 4 products, `isolation` 2 × 3, each with one user per role, with sockets
     disabled and no `OPENAI_API_KEY`; `--profile` has no default. Proof: `test_seed_profiles_*`.
  6. Among 50 tenants × 100 chunks, a search for one tenant returns 8 of its own chunks and none of
     another's (BUILD-SEQUENCE.md:150). Proof: `test_brand_chunk_search_small_tenant`.
  7. A constraint error in a tenant session surfaces in the service as a mapped error, not at teardown
     (KNOWN-ISSUES.md:50). Proof: `test_session_integrity_error_mapped`.
- **Trace:** CANON.md:47, :79, :137; A§5, §6; SUPERSEDED.md:64, :97; KNOWN-ISSUES.md:30-31, :45, :50-52;
  DATABASE.md §4. **Stand-in:** hash-seeded vectors (CANON.md:79). **Waits:** none.

## S11 · Observability spine

- **Depends on:** S8 (the `request_id` columns exist).
- **Scope:** middleware that reads `X-Request-ID` or mints one, keeps it in a contextvar, echoes it,
  binds it into structlog and stamps it on `job_runs`, `agent_runs`, `validation_runs`, `audit_log`
  (BACKEND-ARCHITECTURE.md:2733-2736); JSON logs with Cloud Logging fields; a redaction processor and a
  Sentry `before_send` scrubber, `send_default_pii=False` (ENV.md:120); `SENTRY_DSN` required when
  `APP_ENV != local` (ADR-0013 §1); Sentry FastAPI and SQLAlchemy integrations only (no ARQ or Redis);
  OTel with `/healthz` and `/readyz` excluded (ENV.md:125).
- **Done when:**
  1. A request with `X-Request-ID: abc` returns `abc` in the header, and every log line it emits carries
     it. Proof: `test_request_id_propagates`.
  2. `audit_log` and `job_runs` rows written in that request carry `abc`. Proof: `test_request_id_stamped_on_rows`.
     (Worker re-binding **moves to S13**, KNOWN-ISSUES.md:38.)
  3. Fake `AMAZON_CLIENT_SECRET`, `access_token`, `refresh_token`, `hashed_password`, `token_enc` and
     `Authorization` values come out redacted in logs and Sentry events. Proof: `test_redaction_*`.
  4. `/healthz` is 200 with the DB stopped; `/readyz` is 503 naming Postgres; neither is logged or
     traced. Proof: `test_health_*` (extends S1's tests).
  5. Boot with `APP_ENV=staging` and no `SENTRY_DSN` fails. Proof: `test_config_sentry_required`.
  6. [staging] Logs in Cloud Logging with trace correlation; probes wired to Cloud Run.
- **Trace:** CANON.md:33, :68; ENV.md:120-125; SUPERSEDED.md:24, :32; BACKEND-ARCHITECTURE §11.4; spec
  L6650, L12643. **Stand-in:** in-memory span exporter; empty DSN. **Waits:** Sentry DSN. **Open:** OQ10, OQ11.

## S10 · Auth, RBAC, maker-checker backstop, `0004_integrity`, `0005_rls`

- **Depends on:** S9; T7 on its default (FastAPI issues the JWT).
- **Scope:**
  - `POST /api/v1/auth/login|refresh|logout`, `GET /api/v1/auth/me`; PyJWT HS256 with claims `{sub,
    tid, roles[], email, typ, jti, iss, aud, iat, exp}`; argon2id; 30 min access, 14 day refresh with
    rotation and reuse detection (CANON.md:82; ENV.md:69-75).
  - `get_current_principal` dependency; a role × action matrix; every route declares a role or
    `@public` (BUILD-SEQUENCE.md:166).
  - `0004_integrity` per DATABASE.md:1770 (guards, append-only, last-owner guard, transition table and
    seed, purge/prune functions, and `audit()` + its grant, moved from S6); `0005_rls` ENABLE + FORCE
    on every tenant table, with an emergency disable script in place of a settings flag
    (DATABASE.md:1771).
  - Postgres rate limiter (120/tenant, 60/IP, 5/login per IP and email) taking the client IP from the
    entry Cloud Run appends (ADR-0005; KNOWN-ISSUES.md:37); explicit CORS list; 10 MiB body cap.
- **Done when:**
  1. Login returns exactly the frozen claims with one role. Proof: `test_login_claims`.
  2. A `reviewer` token gets 403 on a `listing:generate` route; a test fails if any route lacks a role
     or `@public`. Proof: `test_rbac_*`, `test_every_route_declares_role`.
  3. A reused rotated refresh token revokes the whole chain; logout revokes. Proof: `test_refresh_reuse_revokes_chain`.
  4. Bypassing the API, setting `approved_by_user_id` to the version's generator or editor is rejected
     by the trigger, and an approver from another tenant fails at the membership FK (CANON.md:97;
     ADR-0006 §4). Proof: `test_db_maker_checker_*`. (The API-level 403 **moves to S19**.)
  5. `UPDATE`/`DELETE` on `audit_log` fail for `app_rw`. Proof: `test_audit_append_only`.
  6. RLS is forced on every table with `tenant_id`; tenant A's GUC sees no row of B through raw SQL.
     Proof: `test_rls_forced_everywhere` (catalog assertion, DATABASE.md:1785).
  7. The 6th login in a minute for one IP+email gets 429; a forged left-most `X-Forwarded-For` does not
     change the bucket. Proof: `test_rate_limit_*`.
  8. Login, failed login, refresh reuse and role change each write an audit row. Proof: `test_auth_audit`.
  9. A tenant's last active owner cannot be demoted or deactivated. Proof: `test_last_owner_guard`.
  10. (moved from S6) Through the 0004 variant guard, children of one style with `M/Navy` and
      `m/navy` collide on the stored `variant_key` (ADR-0007 §4; DATABASE.md:424, :1765). Proof:
      `test_variant_key_casefold_collides`.
  11. (moved from S6) A catalog test proves `app_rw` has no DELETE on `tenants`, `users`,
      `business_parties` and `brand_profiles`, and only column UPDATE on `tenants` (`name`, `tone`,
      `guidelines`, `glossary`) (DATABASE.md:229, :1133, :1150). Proof: `test_privilege_matrix[table]`.
- **Trace:** CANON.md:72, :82, :97-98; ADR-0004 §8, ADR-0005, ADR-0006, ADR-0008; SUPERSEDED.md:74-77;
  KNOWN-ISSUES.md:16, :26, :30, :33-34, :37; DATABASE.md §7.1, §8.1-§8.2; T7, T15, T23, T88.
  **Stand-in:** none. **Waits:** none. **Open:** OQ2, OQ8, OQ12.

## S14a · Rules Engine as a pure function, with its fixture suite

- **Depends on:** S2 (`RuleSet`, `ErrorReport`), S7 (`rules` table for the loader).
- **Scope:** a deterministic engine over a `CanonicalListing` and a pushed `RuleSet` returning an
  `ErrorReport`, with no DB session or model call; `rules_engine.py` hard-banned from `openai` and
  `langgraph` (CANON.md:126); the 5 `RuleDef` types (A§16); "highest enabled version per key" and a
  derived `rules_version` (KNOWN-ISSUES.md:24; T55); a defined `banned_terms` tokenizer
  (KNOWN-ISSUES.md:25); seed rules: Amazon title ≤ 200, Flipkart ≤ 150, banned terms, required
  attributes for 2 categories (A§5's fixture categories until the client names the real ones).
- **Done when:**
  1. A 221-char Amazon title yields the golden `FIELD_TOO_LONG` ErrorReport (spec L6211, L11648; T83).
     Proof: `test_rules_title_too_long_golden`.
  2. A banned term is flagged, including `#1` under `match="word"`. Proof: `test_banned_terms_*`.
  3. A missing required attribute reports `source="rules_engine"`. Proof: `test_required_attribute`.
  4. Hypothesis: the same input always gives the same report; no valid listing raises. Proof:
     `test_rules_engine_properties`.
  5. Branch coverage ≥ 90% (ADR-0010 §2). Proof: coverage in `make api-tests`.
  6. Importing `openai` or a DB module in `rules_engine.py` fails lint. Proof: `make api-lint`, `make lint-imports`.
  7. Nightly mutation score ≥ 80%, or a written waiver [C9] (EP decision 20).
- **Trace:** invariant 2; CANON.md:126; A§4, §16-§17; KNOWN-ISSUES.md:23-25; ADR-0010; T9, T16, T32,
  T36, T47, T48, T55. **Stand-in:** A§5 categories. **Waits:** the 2 launch categories. **Open:** OQ13.

## S12 · Walking skeleton

- **Depends on:** S10, S11; [staging] lines also need S4 and DNS.
- **Scope:** products CRUD through router → service → repository with keyset paging
  (BACKEND-ARCHITECTURE.md:1332-1333); one image with `api` and `worker` roles; migrations as the Cloud
  Run job, never at container start (CANON.md:75); one uvicorn per instance (ADR-0005).
- **Done when:**
  1. Locally, an `acme` operator creates a product stored under `acme`; `bharat` users get 404 for its
     id. Proof: `test_products_crud_*`, `test_products_cross_tenant_404`.
  2. A router importing `sqlalchemy` or `app.db` fails `lint-imports`. Proof: `prove_gates.sh` case.
  3. The built image boots as `api` against compose and `/readyz` is 200. Proof: `make bootstrap`.
  4. [staging] A second person creates a product over TLS at `https://api-staging.<APEX>`, stored in
     staging Cloud SQL under the right tenant (spec L4968).
  5. [staging] A push to `main` runs the migration job before both services deploy.
- **Trace:** spec L4960-4968; CANON.md:42, :75; SUPERSEDED.md:72; ADR-0001, ADR-0009. **Stand-in:**
  compose. **Waits:** billing, domain, DNS. **Open:** OQ14.

## S17a · Pillow image checks as a pure library

- **Depends on:** S2.
- **Scope:** deterministic format, dimension, resolution, aspect-ratio and background checks per
  marketplace and slot (spec L5161, L6421, L6694), returning an `ImageReport` with `checks[]`.
- **Done when:**
  1. A synthetic fixture library gives each check a passing and a failing image; each failure yields its
     frozen code (T84). Proof: `test_pillow_checks_*`.
  2. The report lists every check that ran and serialises `pass`. Proof: `test_image_report_checks_listed`.
  3. The library imports no model client or DB module. Proof: `make lint-imports`.
- **Trace:** CANON.md:128; spec L4761, L5161, L6694; T84. **Stand-in:** generated fixtures. **Waits:**
  real images. **Open:** OQ15.

## S13 · Job spine on procrastinate, and CSV bulk import

- **Depends on:** S8, S11, S12 (local half).
- **Scope:**
  - Queues `default` and `publish`; one helper defers the job in the transaction that writes its rows
    and `job_runs` row; payloads hold only `job_run_id`; the worker loads the tenant from the row
    (ADR-0004 §2, §4).
  - `asyncio.timeout` per kind (generation 300 s, publish 600 s, import 1800 s) → `failed` +
    `JOB_TIMEOUT`; backoff retries for transport/5xx/429; generation `max_attempts=1` for domain failures
    (ADR-0004 §5-§6); a reaper for lost leases; graceful SIGTERM (KNOWN-ISSUES.md:48).
  - `GET /api/v1/jobs/{job_id}`; the dead-letter list and requeue are the staff CLI
    (`python -m app.cli jobs dead-letter list`, `jobs requeue`), not `/api/v1` paths (S13 PLAN D2)
    (new row with `parent_job_id`) (CANON.md:60-61).
  - `POST /api/v1/products/bulk` (Idempotency-Key; 10 MiB multipart or an object key after a signed
    upload, CANON.md:83), header and per-row checks, `parent_sku` (ADR-0007 §2), `ImportReport`;
    `GET /api/v1/imports/{id}`, `.../rows?status=failed`, `POST .../rows/retry`. Import does not start
    generation (T67 default).
- **Done when:**
  1. Rolling back a transaction that deferred a job leaves 0 rows in `job_runs` and the queue table;
     commit leaves 1 in each (spike criterion 1). Proof: `test_transactional_defer`.
  2. A 50-row CSV with 5 bad rows yields 45 products, `failed_rows=5` and 5 `import_rows` with
     `error_report_json` of `source` `schema` or `rules_engine` (spec L4981; A§19). Proof: `test_import_50_rows_5_bad`.
  3. Re-uploading creates no duplicate SKU; a repeated SKU in one file yields `DUPLICATE_SKU_IN_FILE`; a
     missing column yields `HEADER_MISSING_COLUMN` at `file.header`. Proof: `test_import_*`.
  4. Each row-model defect of KNOWN-ISSUES.md:47 has a test. Proof: `test_row_model_*`.
  5. (moved from S8) `GET /api/v1/jobs/{job_id}` returns a `JobEnvelope` from `job_runs`; another
     tenant's id is 404. Proof: `test_job_envelope_from_db`.
  6. (moved from S8) Same Idempotency-Key and body replays the stored response; a different body gets
     409. Proof: `test_idempotency_*`.
  7. A timed-out task ends `failed` with `JOB_TIMEOUT`; retries back off and stop at the cap; a generation
     domain failure is not retried. Proof: `test_job_timeout`, `test_job_retry_policy`.
  8. After `kill -9` mid-job the reaper recovers the job; on SIGTERM jobs finish or release within 10 s.
     Proof: `test_worker_crash_recovery`, `test_worker_sigterm`.
  9. A forced terminal failure gives a `dead_letter` row; requeue inserts a new row with `parent_job_id`
     and a fresh `correlation_id`, leaving the old row unchanged. Proof: `test_dlq_requeue`.
  10. (moved from S11) Worker logs and rows carry the originating `request_id`. Proof: `test_worker_rebinds_request_id`.
  11. `bootstrap` boots the image as `worker`. Proof: `make bootstrap`.
- **Trace:** CANON.md:19-21, :49-52, :60-63, :83; ADR-0004; SUPERSEDED.md:26, :71-75, :125;
  KNOWN-ISSUES.md:27, :36, :46-49; DATABASE.md §6.2, §7.2; spec L4981, L6630, L11345; T3, T67, T82.
  **Stand-in:** MinIO, synthetic CSVs. **Waits:** real client CSVs. **Open:** OQ16, OQ17.

## S14b · AI seam, `/rules/dry-run`, `/internal/admin`, ONBOARDING

- **Depends on:** S3, S9, S10, S13, S14a.
- **Scope:**
  - Backend implementations of the 5 ports; a stub `run_listing_pipeline` returning a valid
    `GenerationResult` from fixtures; the `MarketplaceAdapter` ABC and `FakeAdapter` (CANON.md:129;
    spec L5246-5253); an import-linter contract that the pipeline imports nothing from `app`.
  - `GET /api/v1/rules`, `POST /api/v1/rules/dry-run` (persists nothing).
  - Read-only SQLAdmin at `/internal/admin` through `platform_scope`, `token_enc` and `hashed_password`
    excluded; ops pages for queue, cost, funnel and dead letters (BUILD-SEQUENCE.md:235); the
    `ADMIN_ENABLED` kill switch (A§14) and the admin-secret deploy guard (ADR-0013 §1).
  - `ONBOARDING.md`: invariants, repo tour, how to run, CANON.md:130's ownership line verbatim.
- **Done when:**
  1. With sockets disabled and no GCP or model credentials, `run_listing_pipeline(ctx, deps)` on the stub
     returns a valid `GenerationResult`, every port going through the backend implementation. Proof:
     `test_seam_stub_end_to_end`.
  2. `FakeAdapter` passes the adapter contract suite (spec L5253). Proof: `test_adapter_contract[fake]`.
  3. `POST /api/v1/rules/dry-run` on the 221-char title returns the golden report and changes no row
     count. Proof: `test_rules_dry_run_persists_nothing`.
  4. With A§14's production fixture (reduced to the 3-variable env block and a `postgresql+psycopg` URL,
     CANON.md:21), `/internal/admin` is 404, and `ADMIN_ENABLED=true` in production exits at boot. Proof:
     `test_admin_killswitch`.
  5. The admin renders a seeded listing's `content_json` and ErrorReport; no response holds `token_enc`
     or `hashed_password` values. Proof: `test_admin_read_only_*`.
  6. The `bootstrap` image import adds the pipeline module (ADR-0013 §4). Proof: `make bootstrap`.
  7. Every command in ONBOARDING.md runs on a clean clone. Proof: `test_onboarding_commands`.
  8. [staging] The admin is reachable only through IAM.
- **Trace:** CANON.md:100-130; A§14, §21; SUPERSEDED.md:29-33, :78; ADR-0012 §3, ADR-0013 §1, §4-§5;
  KNOWN-ISSUES.md:31; T104. **Stand-in:** stub pipeline, FakeAdapter. **Waits:** none. **Open:** OQ18, OQ19.

## S15 · Marketplace accounts, OAuth and KMS envelope encryption

- **Depends on:** S10, S13; [staging] lines need S4 and the domain.
- **Scope:**
  - `POST /api/v1/marketplaces/connect`, `GET /api/v1/marketplaces/{marketplace}/callback` with no JWT:
    the tenant comes from the hashed `oauth_states` row (PKCE, 600 s, consumed once with `DELETE …
    RETURNING`) (CANON.md:34; ADR-0004 §8; BACKEND-ARCHITECTURE.md:2122-2130).
  - `KeyWrapper`: `LocalKmsStub` when `APP_ENV=local`, Cloud KMS otherwise, built lazily (ENV.md:60-61);
    a fresh data key per write; `kms_key_version` stored; the API encrypts, the worker decrypts (EP decision 15).
  - Token refresh before `expires_at` and connection health as worker jobs; one refresh, then
    `needs_reconnect` and a reconnect task; revoke nulls ciphertext and key version (T27).
- **Done when:**
  1. Against the fake marketplace server, connect → consent → callback stores an encrypted token for the
     right tenant. Proof: `test_oauth_connect_flow`.
  2. A replayed, expired or unknown state is refused; an `acme` state cannot bind a `bharat` account.
     Proof: `test_oauth_state_*`.
  3. The ciphertext never contains the plaintext; `LocalKmsStub` round-trips with the CI key (A§9).
     Proof: `test_keywrapper_roundtrip`.
  4. Token values never appear in logs, ErrorReports, the admin or a `GenerationContext` (invariant 6).
     Proof: `test_tokens_never_leak`.
  5. A rejected refresh sets `needs_reconnect`, opens one human task and emits the refresh-failure alert
     event (spec L5360). Proof: `test_refresh_failure_reconnect`.
  6. Revoke sets `token_enc` and the key version to NULL. Proof: `test_revoke_crypto_shreds`.
  7. Only adapter and worker modules import the decrypt path. Proof: `make lint-imports`.
  8. [staging] Encrypt/decrypt use Cloud KMS `marketplace-os/oauth-token-key` with the decision 15 grants.
  9. [external] A real Amazon LWA connect through the registered redirect URI (A§26).
- **Trace:** invariant 6; CANON.md:34-36, :70; A§26; ADR-0001, ADR-0004 §8; BACKEND-ARCHITECTURE §3.2,
  §9.4; DATABASE.md §5.4; spec L5262-5263, L11433, L12526; T27, T57, T77. **Stand-in:** fake marketplace
  server, `LocalKmsStub`. **Waits:** Amazon credentials, domain. **Open:** OQ6, OQ20.

## S16 · Brand onboarding and brand-voice vectors

- **Depends on:** S10, S13.
- **Scope:** `GET /api/v1/tenants/me` and a profile update; `POST /api/v1/brand/documents` →
  `brand_documents` row → embedding job (CANON.md:94); chunking per meaningful unit (T90), `content_hash`
  dedupe, `embedding_model` per row; `app/services/embeddings.py` as the only embedding caller and
  `app/services/telemetry.py` writing one `agent_runs` row per call with `tenant_id` (CANON.md:113,
  :126); members and invitations per ADR-0006 §5; owners created by the admin CLI.
- **Done when:**
  1. A brand document produces chunks for its tenant only, each with `embedding_model` and
     `content_hash`; re-uploading it adds none. Proof: `test_brand_document_ingest`.
  2. Every embedding call writes one `agent_runs` row with `tenant_id`, model, tokens and `cost_usd`.
     Proof: `test_embedding_agent_runs`.
  3. `BrandChunkRepository.search` for `acme` never returns `bharat` chunks. Proof: `make tenant-isolation`.
  4. `python -m app.cli` creates a tenant with its first owner (the C6 path). Proof: `test_cli_create_owner`.
  5. [1.1 via C6] Invite create/accept/revoke by email through a provider.
  6. [1.1 via C5] Approved listings fed back as brand chunks (T93).
  7. [external] Real embeddings with the client's model key.
- **Trace:** CANON.md:94, :113, :126, :128; ADR-0006; DATABASE.md §5.1, §5.5; BACKEND-ARCHITECTURE §3.1;
  spec L9379-9405, L11005, L11019; T38, T64, T66, T90, T93. **Stand-in:** a hash-seeded embedder behind
  `embeddings.py` (CANON.md:79). **Waits:** model key, email provider. **Open:** OQ6, OQ21, OQ22.

## S18 · Generation through the AI seam

- **Depends on:** S14b; S16 before its final gates.
- **Scope:** `POST /api/v1/listings/generate` → 202 `JobAccepted` → `default` job → the backend builds
  `GenerationContext` (product, brand profile, `RuleSet`, `CategoryTemplate`, previous version) →
  `run_listing_pipeline` → one transaction writes the version, `validation_runs`, `agent_runs` from
  `GenerationResult.agent_runs[]` and the job status (CANON.md:113, :117); `blocked` after 3 in-graph
  attempts; `POST /api/v1/listings/{id}/regenerate`; `PATCH /api/v1/listings/{id}/content` (new version,
  `edited_by_user_id`); `POST /api/v1/listings/validate`; node events into `job_runs.node` and a span per
  node; the budget checks of ENV.md:90-91 before enqueue.
- **Done when:**
  1. Generate returns 202 and, after the stub run, a version and its `validation_runs` exist and the job
     is `succeeded`. Proof: `test_generate_happy_path`.
  2. A stub fixture failing 3 times leaves the listing `blocked` with the full ErrorReport and
     `attempts_used=3`, with no further attempt. Proof: `test_generate_blocked_after_3`.
  3. `agent_runs` rows equal the stub's records, one per call. Proof: `test_agent_runs_one_per_call`.
  4. The serialised `GenerationContext` holds no marketplace token or account field. Proof:
     `test_context_has_no_credentials`.
  5. `PATCH .../content` makes a new version, keeps `generated_by_user_id`, sets `edited_by_user_id`.
     Proof: `test_patch_content_versioning`.
     Proved on S18's routes; S19 adds the review-side tests (`test_review_on_s18_routes.py`).
  6. `/listings/validate` returns 200 with `passed: false` on a rules failure. Proof: `test_validate_200_failed`.
  7. A crashed generation job leaves the listing out of `generating` via the reaper. Proof: `test_generation_reaper`.
  8. At the tenant budget cap, generate is refused with an ErrorReport and nothing is enqueued. Proof:
     `test_budget_kill_switch`.
  9. [1.1 via C2] "Ask one question, then resume": v1 sets `blocked` + ErrorReport and the user edits or
     regenerates; `human_tasks` still ships.
  10. [external] The same tests pass with the AI team's pinned pipeline in place of the stub (T104).
- **Trace:** invariants 1, 7, 9; CANON.md:54, :113-125; ADR-0004 §6; BACKEND-ARCHITECTURE §3.4, §6, §8.3;
  spec L5213, L6916, L11359-11391; T67, T79, T80, T89, T100. **Stand-in:** stub pipeline. **Waits:** AI
  drop (due D29). **Open:** OQ3, OQ23.

## S19 · Review and approval

- **Depends on:** S18.
- **Scope:** `POST /api/v1/listings/approve` (approve or reject) with the JWT subject as approver and any
  client reviewer id ignored, targeting the latest version, writing `review_decisions` and an audit entry
  (ADR-0008 §3; DATABASE.md §7.1); reject per T40's default (`blocked` + `status_reason 'rejected'`,
  required reason and feedback, optional `regenerate=true`); detail and audit-history reads;
  `GET /api/v1/issues`, `POST /api/v1/issues/{id}/dismiss` (DATABASE.md:1327-1328).
- **Done when:**
  1. (moved from S10) The generator, or the latest version's editor, approving gets 403 with a
     well-formed ErrorReport. Proof: `test_api_maker_checker_*`.
  2. Approving a superseded version is refused. Proof: `test_approve_stale_version`.
  3. A one-member tenant can never approve (T15); a second `reviewer` or `owner` can; `admin` cannot
     (T88 default). Proof: `test_approver_roles`.
  4. Hypothesis over generate/edit/regenerate/approve sequences: no maker approves their own version.
     Proof: `test_maker_checker_property`.
  5. Reject without reason or feedback is refused; `regenerate=true` moves the listing to `generating`
     in the same transaction. Proof: `test_reject_*`.
  6. `GET /api/v1/issues` lists only the caller's open issues, keyset-paged. Proof: `test_issues_inbox`.
  7. [1.1 via C4] Batch approve.
  8. [1.1 via C3] `GET /api/v1/errors` analytics.
- **Trace:** invariant 3; CANON.md:97; ADR-0006 §4, ADR-0008; BACKEND-ARCHITECTURE §3.5, §9.3;
  DATABASE.md §5.3, §5.7, §6.1; spec L6934, L11405, L11489; T15, T40, T42, T88, T91. **Stand-in:** none.
  **Waits:** none. **Open:** OQ2, OQ6.

## S20 · Adapter framework and the Amazon adapter

- **Depends on:** S14b, S15.
- **Scope:** the ABC returns a per-field `ListingResult`, never a boolean (spec L10519-10533; T86), with
  the proposed network-free `preview()` and outbound limiter per (marketplace, account, operation)
  (BACKEND-ARCHITECTURE.md:1204); Amazon SP-API Listings mapping for the 2 categories, families as one
  unit (ADR-0007 §3; T45), value maps (T49), `marketplace_error_map` → ErrorReport with
  `source="amazon_adapter"`, 429 backoff.
- **Done when:**
  1. The Amazon adapter passes the contract suite against the fake server. Proof: `test_adapter_contract[amazon]`.
  2. A fake response rejecting 2 of 5 fields gives 3 `succeeded` and 2 `failed` fields, each failure an
     ErrorItem with the raw code. Proof: `test_amazon_partial_result`.
  3. A 429 is retried with backoff and the limiter never exceeds its rate. Proof: `test_amazon_throttling`.
  4. An unmapped marketplace code still yields a valid ErrorReport. Proof: `test_amazon_unknown_error`.
  5. `CanonicalListing` has no marketplace-specific field and only adapters import the marketplace HTTP
     client (invariant 4). Proof: `test_canonical_has_no_marketplace_fields`, `make lint-imports`.
  6. A size × colour family is one adapter operation. Proof: `test_amazon_family_payload`.
  7. [external] One sandbox listing published with per-field results (gate 5).
- **Trace:** invariants 1, 4; spec L5246-5253, L6778, L10519-10533, L12017; ADR-0007; DATABASE.md §5.3;
  T45, T49, T53, T86. **Stand-in:** fake server, FakeAdapter. **Waits:** Amazon credentials (T77).

## S21 · Publish, per-field retry, status sync

- **Depends on:** S19; S20 before its final gates (FakeAdapter first).
- **Scope:** `POST /api/v1/listings/publish` (owner or operator, Idempotency-Key, 202, `publish` queue,
  gate `approved_version_id = current_version_id`, BACKEND-ARCHITECTURE.md:1862); a write-ahead
  `listing_submissions` row per call and append-only `publish_field_results`;
  `POST /api/v1/listings/{id}/publish/retry-fields` sending only failed fields with `parent_job_id`; at
  most 3 submissions per approved version (T19); read-before-retry; bounded status sync (T18) and
  "Submitted" until the marketplace confirms (T13).
- **Done when:**
  1. A listing not `approved` at its current version cannot publish; a `reviewer` gets 403. Proof: `test_publish_gate_*`.
  2. With FakeAdapter failing 2 of 5 fields the job ends `partial`; the retry sends only those 2. Proof:
     `test_publish_partial_retry`.
  3. Same Idempotency-Key and body replays; a different body gets 409. Proof: `test_publish_idempotency`.
  4. A 4th submission for one approved version is refused and the listing is `blocked`. Proof: `test_submission_cap`.
  5. `kill -9` between the pending-submission commit and the response yields no duplicate submission
     (DATABASE.md:1789). Proof: `test_publish_crash_no_duplicate`.
  6. Of two concurrent publish jobs on one listing, exactly one proceeds. Proof: `test_publish_race`.
  7. `published` is not shown as live until a sync confirms; exhaustion follows T18. Proof: `test_status_sync_*`.
  8. The previous worker handles jobs the new API enqueues (EXECUTION-PLAN.md:291-293). Proof: `test_job_payload_compat`.
  9. [1.1 via C4] Batch publish.
- **Trace:** invariants 1, 10; CANON.md:61; ADR-0004 §7; BACKEND-ARCHITECTURE §3.6, §8.4-§8.5;
  DATABASE.md §5.3, §6.4, §7.3; spec L6940, L11419, L12017-12033; T13, T18, T19, T46, T51.
  **Stand-in:** FakeAdapter, fake server. **Waits:** none locally. **Open:** OQ24.

## S17b · Image pipeline (minimal)

- **Depends on:** S13, S17a.
- **Scope:** `POST /api/v1/images/upload` with a signed upload step, `images` rows, a Pillow job, the
  stored `ImageReport`, `GET /api/v1/images/{id}/report`, bucket lifecycle; vision results as warnings
  (C8); `deps.validate_images` returns the stored reports (CANON.md:119).
- **Done when:**
  1. An image uploaded to MinIO through a signed URL gets a row, a Pillow job and a report. Proof: `test_image_upload_flow`.
  2. Another tenant's image id is 404 for metadata and report. Proof: `test_image_cross_tenant`.
  3. A vision failure is a warning and does not block approval [C8]. Proof: `test_vision_is_warning`.
  4. The storage client refuses `local_only_` values outside `APP_ENV=local` (KNOWN-ISSUES.md:66-67).
     Proof: `test_storage_rejects_local_values`.
  5. [staging] Upload and lifecycle on the staging bucket.
  6. [1.1 via C8] Vision results that block approval.
- **Trace:** CANON.md:58, :119, :128; ENV.md:47-49; spec L4761, L11461; T84. **Stand-in:** MinIO.
  **Waits:** none. **Open:** OQ6.

## S22 · Flipkart adapter, behind a flag

- **Depends on:** S15, S21.
- **Scope:** mapping, throttling, error mapping (`source="flipkart_adapter"`), families without a parent
  SKU (DATABASE.md:1793); off by default at launch (C7).
- **Done when:**
  1. The adapter passes the contract suite against the fake server and recorded fixtures. Proof:
     `test_adapter_contract[flipkart]`.
  2. With the flag off, Flipkart connect and publish are refused with an ErrorReport and no call leaves
     the process. Proof: `test_flipkart_flag_off`.
  3. A family with no parent SKU produces a valid payload. Proof: `test_flipkart_family`.
  4. [external] One supervised publish with real credentials [C7] (T78).
- **Trace:** spec L5292-5328, L6778; EXECUTION-PLAN.md:642, :701; T78. **Stand-in:** fixtures,
  FakeAdapter, fake server. **Waits:** Flipkart credentials. **Open:** OQ25.

## S23 + S24 · Analytics (4 metrics) and observability completion

- **Depends on:** S21, S11.
- **Scope:** `GET /api/v1/analytics` with the plan's 4 operational metrics (generation volume, approval
  rate, validation pass rate, publish success rate) and a marketplace breakdown (spec L4773, L5370,
  L6956; T14); a cost view per listing, tenant and model from `agent_runs` (spec L12705) through a named
  rollup module allowed `platform_scope` (KNOWN-ISSUES.md:31); Sentry releases from `GIT_SHA`; a
  click-to-marketplace trace (spec L5385); alerts for refresh failure, dead letters, error rate and
  budget (BACKEND-ARCHITECTURE.md:2764-2773).
- **Done when:**
  1. On a fixed seeded dataset the 4 metrics equal hand-computed values per tenant and include no other
     tenant's rows. Proof: `test_analytics_metrics`, `make tenant-isolation`.
  2. Cost per listing equals the sum of its `agent_runs.cost_usd`. Proof: `test_cost_rollup`.
  3. A forced failed publish gives one trace from the API request through the job to the adapter call,
     every span with the same `request_id`. Proof: `test_failed_publish_single_trace` (in-memory exporter).
  4. Each alert condition emits its alert event. Proof: `test_alert_events_*`.
  5. [staging] One Cloud Trace view of that publish; Sentry events carry the release (gate 7).
  6. [1.1 via C1] The 10 BRD metrics (T14).
- **Trace:** spec L4690, L4773, L5360-5385, L6790, L6956, L12657-12705; ENV.md:120-126;
  BACKEND-ARCHITECTURE §11.4; T14. **Stand-in:** in-memory exporter; alerts asserted as events.
  **Waits:** Sentry project, alert webhook. **Open:** OQ10, OQ26.

## S25 · Production via OpenTofu, `deploy-prod.yml`, release and drills

- **Depends on:** S4, the domain; GitHub Team and the bot account before any prod credential (STATE
  open decision 4).
- **Scope:** `infra/envs/prod`; `deploy-prod.yml` on `release: published` with the guards of
  EXECUTION-PLAN.md:287-290; zero traffic, smoke, 15-minute watch, auto-rollback (ADR-0009 §2); `v0.1.0`;
  rollback and PITR drills.
- **Done when:**
  1. `tofu fmt -check` and `tofu validate` pass for `infra/envs/prod`, under the S4 infra guards. Proof:
     make target, `test_infra_guards`.
  2. `deploy-prod.yml` refuses a non-SemVer tag, a tag off `main`, a missing `staging-verified` tag or an
     open `nightly-red` issue. Proof: `test_workflows` cases.
  3. The migration-compatibility test runs the previous release's tests on the new schema (ADR-0009
     Consequences; EXECUTION-PLAN.md:302-305). Proof: `make api-tests`.
  4. [staging] [user-go] `v0.1.0` reaches prod through zero traffic, smoke, shift and watch.
  5. [staging] Rollback to the previous tag completes in under 15 minutes (spec L5424; gate 10).
  6. [staging] A PITR restore is measured against gate 6 (spec L6784; T87).
- **Trace:** ADR-0001, ADR-0009; EXECUTION-PLAN §4.5-§4.6, decisions 12-14, 21; spec L5424, L6742-6808;
  T87. **Stand-in:** none. **Waits:** billing, domain, GitHub Team, bot account. **Open:** OQ27.

## S26 · Launch hardening

- **Depends on:** every step above.
- **Scope:** failure injection (rate limits, token expiry, partial publish, network drop, duplicate jobs;
  spec L6694); load test; security, tenancy and secrets audit; prompt-injection test with the AI team
  (spec L5395, L12615); the BRD §21 walkthrough as an automated E2E (ADR-0010 §1; T8); prod smoke;
  production OAuth check; runbooks and incident response (spec L13671).
- **Done when:**
  1. Each injected fault ends in a documented state with an ErrorReport and no duplicate marketplace
     write. Proof: `test_failure_injection_*`.
  2. Locally, a 1,000-row CSV plus 20 concurrent generations on the stub finish with zero dropped jobs
     and no queue starvation (spec L5407, L5411, L6796). Proof: the plan's load script.
  3. The automated BRD §21 walkthrough passes locally (DATABASE.md:1796). Proof: `test_brd_walkthrough`.
  4. Malicious titles and descriptions change no rules outcome and leak no other tenant's data. Proof:
     `test_prompt_injection_*`.
  5. The security audit (RBAC, cross-tenant, maker-checker, KMS, CORS, TLS) is recorded with evidence
     and 0 open P0/P1 findings (spec L5411).
  6. Core-module mutation score ≥ 80%, or a written per-release waiver [C9].
  7. [staging] Load test and walkthrough pass on staging (gates 1, 8, 9).
  8. [external] Production OAuth with real credentials; UAT with 2 client people if T8 keeps it.
- **Trace:** spec L5395-5411, L6694, L6742-6808, L12615, L13671; ADR-0010; EXECUTION-PLAN §6.4-§6.6; T8,
  T81, T82, T87. **Stand-in:** stub pipeline, FakeAdapter, fake server. **Waits:** pilot brand, UAT
  people. **Open:** OQ28.

---

## Cuts and what they move to 1.1

| Cut | Moves to 1.1 | Marked at |
|---|---|---|
| C1 | The 10 BRD analytics metrics (T14) | S23+S24 item 6 |
| C2 | "Ask one question, then resume" (tables still ship) | S18 item 9 |
| C3 | `GET /api/v1/errors` analytics (`GET /issues` ships) | S19 item 8 |
| C4 | Batch approve and batch publish (T42) | S19 item 7, S21 item 9 |
| C5 | Approved-listing memory (T93) | S16 item 6 |
| C6 | Invites and email; owners via the admin CLI | S16 items 4-5 |
| C7 | Flipkart behind a flag, one supervised publish | S22 |
| C8 | Blocking vision results; Pillow ships | S17b items 3, 6 |
| C9 | Mutation-score waiver per release | S14a item 7, S26 item 6 |
| C10 | BRD detail gaps (multi-GSTIN, size charts…) | no criterion; each plan lists its own |

Source: EXECUTION-PLAN.md:697-702. Each cut needs the client's OK (T16, VISION Q25).

## Open questions

"Not specified" in the sources, or the sources disagree. The named step's plan resolves each with the
Tech Lead, adding a TEAM-QUESTIONS row where the client or another team decides.

- **OQ1 (S2, S7):** a persisted state for a rejected and for a partially published listing
  (KNOWN-ISSUES.md:15); T40's default covers rejection; confirm before the freeze.
- **OQ2 (S2, S10, S19):** no maker-checker or stale-version `ErrorCode`, and no `request`/`listing` path
  (KNOWN-ISSUES.md:26; BACKEND-ARCHITECTURE.md:1404-1406). New codes need the user's OK.
- **OQ3 (S2, S18):** the mapping from `GenerationResult.terminal_state` to `listings.status` (KNOWN-ISSUES.md:27).
- **OQ4 (S2):** the D14 MAJOR window is today (Sun 4 Oct) and S2 is unbuilt. May `schemas-v1.0.0`, the
  first release after the empty 0.0.0, ship outside a window, or does it wait for D28?
- **OQ5 (S3):** the mock-server tool, its pin, and whether it sends telemetry (`docs/RULES.md` §9; ADR-0005).
  The draft names prism (BUILD-SEQUENCE.md:48).
- **OQ6 (S3, S15–S17b, S19):** paths not specified: tenant switch, members and invitations,
  brand-profile update, connected accounts and health, the image upload-URL step, listing detail and
  audit history, and whether `connect` carries `{marketplace}` (BACKEND-ARCHITECTURE.md:1327-1347);
  default and maximum page size (BACKEND-ARCHITECTURE.md:1415).
- **OQ7 (S6):** whether product request/response models belong in `mkos-schemas` (BUILD-SEQUENCE.md:99);
  the 17-module layout has no products module.
- **OQ8 (S7, S10):** KNOWN-ISSUES.md:34 asks for a purge/offboarding ADR before `0004`; DATABASE.md rev 3
  designs `purge_tenant()`, but no ADR number is claimed.
- **OQ9 (S4):** A§26 turns the `<APEX>` grep into a hard failure once a staging deploy workflow exists,
  so `deploy-staging.yml` cannot merge before the apex is chosen (STATE open decision 5).
- **OQ10 (S11, S24):** trace export: ADR-0001 says OTLP to Cloud Trace, ENV.md:123 says the direct
  exporter (BACKEND-ARCHITECTURE.md:2746-2748).
- **OQ11 (S11):** W3C `traceparent` in job payloads (VISION §4.12) versus "payloads carry only the
  `job_run_id`" (ADR-0004 §4).
- **OQ12 (S10):** with global users (ADR-0006), which tenant login issues a token for when a user has
  several memberships, and the tenant-switch carve-out from "tenant only from the JWT" (`docs/RULES.md` §3), which needs an ADR
  (DATABASE.md:309, :1955).
- **OQ13 (S14a):** the engine's signature: the draft has `(listing, marketplace, category)`
  (BUILD-SEQUENCE.md:235); with `RuleSet` pushed (A§17) nothing freezes it.
- **OQ14 (S12):** the site's proof is a teammate logging in and creating a product over TLS (L4968);
  with no FE app in this repo, is an API-level proof enough?
- **OQ15 (S17a):** Pillow thresholds per marketplace and slot, and whether they live in `rules` data.
- **OQ16 (S13):** the worker health port: a worker pool needs no `$PORT` (CANON.md:25), CANON.md:63 asks
  for a health server, and locally it collides with the API on 8000 (KNOWN-ISSUES.md:36, :49). A
  `WORKER_HEALTH_PORT` would be a new env row.
- **OQ17 (S13):** who may requeue dead letters in production (VISION Q18; T82; ENV.md:24).
- **OQ18 (S14b):** where the stub pipeline lives and whether it imports as `mkos_pipeline` (ADR-0012 §3;
  ADR-0013 §4).
- **OQ19 (S14b):** the draft's separate `api-admin` Cloud Run service (BUILD-SEQUENCE.md:235) is not in
  ADR-0001's service list.
- **OQ20 (S15):** the token cipher (AES-256-GCM, tenant as AAD) awaits a crypto ADR
  (BACKEND-ARCHITECTURE.md:2125).
- **OQ21 (S16):** no local embedding stand-in is named (PHASES.md:77); this file proposes the seed's
  hash-seeded vectors.
- **OQ22 (S16):** brand-document size limits and accepted file types.
- **OQ23 (S18):** the budget kill-switch error code; `BUDGET_EXCEEDED` is only in drafts and
  BACKEND-ARCHITECTURE.md:1793, not in `codes.py`.
- **OQ24 (S21):** whether the first field retries run inside the job or on the user's click
  (BACKEND-ARCHITECTURE.md:1864).
- **OQ25 (S22):** the Flipkart flag: ENV.md has no row or setting for it.
- **OQ26 (S23):** the analytics sub-routes are draft proposals only (BACKEND-ARCHITECTURE.md:1353).
- **OQ27 (S25):** auto-rollback thresholds (EP decision 21, open) and the prod smoke path
  (EXECUTION-PLAN.md:295-299).
- **OQ28 (S26):** the load-test tool: the site names Locust (L5407); there is no pin or ADR.
- **OQ29 (all):** approving this file also approves moving CD into S4, splitting S14 into S14a/S14b
  (EXECUTION-PLAN.md:626), the "done (local)" status and the moved criteria.
