> **ARCHIVED 2026-09-26. Do not follow.** Superseded by `AGENTS.md`. Hosting is under review (see `docs/STATE.md`), and ADR-0001 does not exist.

# AI Marketplace OS — Backend & Database

> **Read `docs/context/CANON.md` before writing any code.** It is the single authoritative
> register for naming, migrations and environment. Where anything else in this repo or in the
> architecture spec disagrees with the Canon, **the Canon wins.**

---

## What we are building

An **AI-powered marketplace listing operator for D2C brands.** Brands connect their Amazon and
Flipkart seller accounts, upload products once, and AI writes the listing (title, bullets,
description, keywords, attributes) for each marketplace in that brand's own voice. Deterministic
code then validates it, a human approves it, and only then does an adapter publish.

**The governing principle — everything in the design follows from this:**

```
AI proposes  ->  deterministic rules validate  ->  humans approve  ->  adapters execute  ->  performance feeds back
```

AI never has publish authority. That safety chain is the product, not the writing.

---

## Our scope

| Team | Owns |
|---|---|
| **US — Backend + Database** | FastAPI (Python 3.11), PostgreSQL 16 + pgvector, Alembic, Redis/ARQ workers, marketplace adapters, Rules Engine, auth/RBAC, the shared contracts package |
| AI team (separate) | LangGraph graph, Content/Keyword/Attribute/Image agents, prompts, Brand RAG. Lives in `packages/pipeline` |
| Frontend team (separate) | Next.js 14 dashboard in `apps/web` |

Three teams build in parallel, so **the contracts between us are the critical path** — not our own
features. `packages/schemas` and the frozen `/api/v1` OpenAPI surface unblock both other teams
simultaneously. Build them first even though they feel like paperwork.

---

## Where we are

- **Sprint:** Mon 21 Sep 2026 → Wed 4 Nov 2026 (45 calendar days)
- **Today is roughly Day 6 of 45** — about 39 days to launch
- Foundation phase: the backend/DB skeleton so feature tickets can be delegated
- Nothing is built yet unless this repo says otherwise

> Dates above are computed from a fixed sprint start; re-check against the real calendar.

---

## Stack (decided — do not relitigate without an ADR)

| Layer | Choice |
|---|---|
| API | FastAPI + Pydantic v2, Python **3.11** exactly |
| DB | PostgreSQL 16 + pgvector (HNSW, 1536-dim), SQLAlchemy 2.0 async + Alembic |
| Async | Redis + ARQ |
| Cloud | **GCP** — Cloud Run, Cloud SQL, Memorystore, GCS, Secret Manager, Cloud KMS |
| Region | `asia-south1` (Mumbai) |
| Packaging | `uv` workspace, `uv.lock` committed, `uv sync --frozen` in CI |
| Contracts | `mkos-schemas` (import `mkos_schemas`, TS `@mkos/schemas`) |

The original spec text mentions **AWS** in the Security/Infrastructure sections. That is superseded —
GCP wins (ADR-0001).

---

## Hard rules (the 10 invariants — never cut these under deadline pressure)

1. **AI never publishes.** Only adapters publish, only after human approval.
2. **The Rules Engine is deterministic code, never an LLM call.** It is the final arbiter.
3. **Human approval is mandatory.** Zero auto-publish in Phase 1.
4. **Marketplace logic stays inside adapters.** The canonical listing holds no marketplace-specific fields.
5. **`tenant_id` scoping on every query**, enforced at the repository base class. No exceptions.
6. **Marketplace credentials never enter an LLM prompt.** KMS-encrypted at rest.
7. **Every AI call persists an `AgentRun`** — model, tokens, cost, duration, status.
8. **One `ErrorReport` shape** for rules, vision, model and marketplace failures alike.
9. **Bounded retries — max 3**, then `BLOCKED`. No infinite loops.
10. **Idempotent mutations; partial retries** touch only the failed field.

---

## Canon quick reference

| Thing | Canonical | Why |
|---|---|---|
| API path prefix (all business endpoints) | `/api/v1` | S3, D4, the contracts poll_url, API_V1_PREFIX and NEXT_PUBLIC_API_BASE_URL already say /api/v1; only the endpoint tables disagree and they are the cheapest thing to find-replace. |
| Unversioned endpoints (the only ones outside the prefix) | `/healthz, /readyz, /openapi.json, /docs, /internal/admin` | A liveness probe must never move with an API version, and /api/v1/health does not exist — the bootstrap verify step that curls it is wrong. |
| Marketplace resource path segment | `marketplaces (plural)` | Every collection noun in the surface is plural (products, listings, jobs, images); one exception is how a typo reaches an external OAuth registration. |
| OAuth callback route shape | `GET /api/v1/marketplaces/{marketplace}/callback` | Resource-then-verb matches /marketplaces/{marketplace}/categories in the same table, and two of the three existing drafts already nest the marketplace first. |
| Amazon redirect URI — the EXACT string submitted inside the SP-API developer application | `local: http://localhost:8000/api/v1/marketplaces/amazon/callback \| staging: https://api-staging.<domain>/api/v1/marketplaces/amazon/callback \| prod: https://api.<domain>/api/v1/marketplaces/amazon/callback` | Amazon re-review is a 2–4 week queue; all three strings are registered on day one so no later environment needs a resubmission. |
| Flipkart redirect URI — exact string for the seller API application | `local: http://localhost:8000/api/v1/marketplaces/flipkart/callback \| staging: https://api-staging.<domain>/api/v1/marketplaces/flipkart/callback \| prod: https://api.<domain>/api/v1/marketplaces/flipkart/callback` | Same rule as Amazon — one frozen string per marketplace per environment, changed only by re-application. |
| Shared contract package — Python distribution name | `mkos-schemas` | The package is actually built as mkos-schemas with wheel path src/mkos_schemas; apps/api's dependency on marketplace-os-schemas is what makes `uv sync --frozen` fail on the first morning. |
| Shared contract package — Python import name | `mkos_schemas` | Matches the hatchling wheel target and the bare-venv import test in S2's done_when. |
| Shared contract package — TypeScript package name | `@mkos/schemas` | apps/web consumes it as a pnpm workspace dependency ("@mkos/schemas": "workspace:*"), not a relative import across app boundaries. |
| packages/schemas module layout | `packages/schemas/src/mkos_schemas/{__init__,base,enums,codes,paths,listing,errors,images,agent,validation,jobs,publish,imports,auth,pipeline}.py` | The mkos_schemas layout is the one with real code behind every file; the other tree exists only as a directory listing. |
| openapi.json — the one path | `packages/schemas/jsonschema/openapi.json` | It is generated output and belongs beside the other generated JSON Schema; the Makefile, the drift gate and `make mock` must all point at one file or two of the three break. |
| Generated TypeScript — the one path | `packages/schemas/ts/src/api.d.ts (from openapi.json) and packages/schemas/ts/src/contracts.d.ts (from jsonschema/*.schema.json), both re-exported by packages/schemas/ts/src/index.ts` | The schemas-drift gate diffs `packages/schemas/jsonschema packages/schemas/ts`; anything generated outside that pair is silently unguarded. |
| Both generated artifacts are committed and machine-owned | `committed, marked linguist-generated, regenerated only by `make -C packages/schemas all`, never hand-edited` | The drift gate is a required check; a hand-edit is meant to turn the build red. |
| AI package — distribution / import name and home | `packages/pipeline, dist mkos-pipeline, import mkos_pipeline, added to [tool.uv.workspace] members` | Only a separate workspace package makes the import-linter contract (packages.pipeline may not import apps.api) and the "AI opens no DB session" rule mechanically enforceable; the other two homes are inside the backend package. |
| Tenant scoping column — one name system-wide | `tenant_id` | D2 already ruled it; in MVP one tenant == one brand, and a three-team rename in week 3 is the alternative. |
| products uniqueness constraint | `UNIQUE (tenant_id, sku)` | Tenant-scoped SKU uniqueness is what the two-tenant seed proves (both tenants hold SKU-001); a global unique makes the second customer fail to onboard. |
| Async job table | `job_runs` | job_runs is the plan's name, it is already the FE's polling contract in S8, and the duplicate CREATE TABLE jobs is what aborts `alembic upgrade head` with 'relation already exists'. |
| Job state column name | `status` | Matches listings.status and the JobEnvelope field the FE badge renders; `state` appears only in the admin draft. |

Full register (52 rows) in `docs/context/CANON.md`; the 26 closing rulings in
`docs/context/CANON-ADDENDUM.md`.

---

## Where things live

| Thing | Where |
|---|---|
| **The Canon (read first)** | `docs/context/CANON.md` |
| Closing rulings on residual conflicts | `docs/context/CANON-ADDENDUM.md` |
| Build order S1–S14 | `docs/context/BUILD-SEQUENCE.md` |
| Open decisions + recommended defaults | `docs/context/DECISIONS.md` |
| Blockers and what we need from the client | `docs/context/BLOCKERS.md` |
| Every environment variable | `docs/context/ENV.md` |
| Cross-team JSON contracts | `docs/context/CONTRACTS.md` |
| Per-area checklists (202 items) | `docs/context/CHECKLISTS.md` |
| "Foundation done" test | `docs/context/DELEGATE-GATE.md` |
| Product/architecture overview | `docs/context/ARCHITECTURE.md` |
| Ready-to-drop starter files | `scaffold/` |
| The shareable kickoff pack (.docx) | `deliverables/` |
| Full 233-spec architecture source | https://system-arch.vercel.app (`/llms-full.txt`, `/content.json`) |

---

## Working agreements

- **Start from `docs/context/BUILD-SEQUENCE.md`.** It is dependency-ordered; do not jump ahead.
- **Every decision becomes an ADR** in `docs/adr/`. If you deviate from the Canon, that is an ADR
  and a PR against `CANON.md` — not a silent local choice.
- **Never weaken tenant isolation** to make a test pass. Six tables inherit tenant scope only
  through a join in the original spec; the Canon adds a denormalised indexed `tenant_id`. Keep it.
- **The AI team and FE team consume our contracts.** Changing `packages/schemas` is a breaking
  change for two other teams — follow the semver policy in `CONTRACTS.md`.
- Additive changes only; do not remove an endpoint or a contract field without a migration plan.

---

## Do not

- Do not invent marketplace rules — they live as **data** in the `rules` table, never in code.
- Do not put marketplace-specific fields in the canonical listing.
- Do not let an LLM see credentials, tokens or another tenant's data.
- Do not add a marketplace beyond Amazon + Flipkart in Phase 1.
- Do not introduce Kubernetes, Kafka, a separate vector DB, or model fine-tuning. All are named
  non-goals.
