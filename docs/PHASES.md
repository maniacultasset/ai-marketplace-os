# Phases and build status

Purpose: where the backend and database build stands, phase by phase, from setup to the v1 launch and beyond.

Read with: `docs/STATE.md` (live status), `docs/EXECUTION-PLAN.md` §3 and §6, `docs/STEPS.md` (scope, "Done when", approved 2026-10-04), `docs/source/system-arch-llms-full.txt` (L<line>), `docs/review/TEAM-QUESTIONS.md`, `docs/adr/README.md`.

**This file is a summary.** The documents above remain the single source of truth; where they
disagree with this file, they win and this file is corrected. Step status here mirrors
`docs/STATE.md` exactly. **Update PHASES.md in the same PR whenever a step changes status.**
Abbreviations: EP = `docs/EXECUTION-PLAN.md`; "EP dec. N" = EP §2; "STATE dec. N" = STATE "Open
decisions" (different numberings).

## Phases at a glance

| Phase | Scope | Status |
|---|---|---|
| 0 | Setup before coding: repo, tools, cloud projects, decisions | done except the open items below |
| 1 (v1) | The site's Day-45 Definition of Done (L4679–4690) within the Phase-1 scope lock (L4731–4799); steps S1–S26 | S1 merged; the rest not started |
| 2, 3 | Deferred by the site ([202] L13283, [203] L13303) | out of v1 scope |

Sprint: Mon 21 Sep → Wed 4 Nov 2026; Day N = date − 20 Sep 2026. Source: STATE "Where we are".

## Phase 0: setup

Source: EP §3; STATE header, "Where we are" (Phase 0), "Decided", "Environment facts"; `docs/adr/README.md`; `git log`.

**Done:** private repo `maniacultasset/ai-marketplace-os`, squash-only, PRs #1 and #2 merged
(README rewrite; backend-only scope, ADR-0012). Local toolchain installed and PostgreSQL 17 +
pgvector smoke-tested (STATE "Environment facts"). GCP projects `aimos-staging` and `aimos-prod` created. ADRs 0001–0005, 0012 and
0013 accepted; 0006–0011 proposed (built as written until the team rules). Docs foundation complete
(2026-09-28). Spec snapshot re-checked unchanged on 2026-09-30.

**Open:**

| Item | Owner | Needed for | Source |
|---|---|---|---|
| GCP billing account, linked to both projects | client | S4 apply, thin staging deploy, S12, S25 | STATE "GCP account", dec. 5; EP P0-C1, P0-C3 |
| Apex domain + DNS (agents never pick one) | user → client | S12 TLS, OAuth redirect URLs | STATE dec. 5; EP P0-C5 |
| Amazon SP-API and Flipkart developer applications | client | S15, S20, S22 | STATE "Start here" 4; T77, T78 |
| GitHub Team plan (checks are advisory on Free) | client | before the first prod release | STATE "Setup decisions"; ADR-0013 §5 |
| Bot account for pushes | user | before any prod credential exists | STATE "Setup decisions" |
| ADR-0004 spike criterion 6 (throughput on Cloud SQL); the local half passed 7 of 8 and decided T3 (procrastinate) on 2026-10-04 | agent, after billing | S8, S13 | EP P0-A5; STATE "Decisions of 2026-10-04" |
| Per-worktree ports so two tracks can run (ADR-0011 amendment accepted 2026-10-04) | agent | Track B | ADR-0011 amendment |
| Launch scope: we plan Amazon-first 4 Nov (user, 2026-10-04); the client confirms | client | — | STATE "Decisions of 2026-10-04"; EP dec. 9 |
| Python 3.13, pipeline repo, drop date | AI team | S2, S13, S18 | STATE dec. 9; T1, T104; EP P0-U9 |
| Also open: P0-A3 (rest), P0-A4 (P12), P0-A6, P0-G2, P0-G3, P0-C2, P0-C4, P0-U2 (OpenTofu), P0-U3, P0-U4, P0-U7, P0-U8 | see EP §3 | various | STATE "Where we are" (Phase 0) |

## Phase 1 (v1): build steps

Source: EP §3.2 P0-C5, §6.2 (S1–S14), §6.3 (S15+), §10; STATE "Start here" 2; ADR-0012 §3; `docs/canon/ENV.md`:10, :60–61.
Rows follow the EP §6.2–§6.3 dependency order, not the build order (STATE "Start here" 2).
Every step runs locally on Docker PostgreSQL 17 + pgvector; Cloud SQL is for staging and prod only (STATE "Decided", superseded 2026-09-29).

| Step | Delivers | Starts after | Local stand-in | Waits on (external) | Status |
|---|---|---|---|---|---|
| S1 | uv workspace, FastAPI with health probes, local DB stack, 10 required checks | Docker, git, repo | — | — | merged (PR #3, 2026-09-30) |
| S5 | Alembic, `0000_bootstrap.sql`, DB roles | S1, P0-U10 first half | — | STATE dec. 10 | done (local): S5a merged (PR #10), S5b on `feat/s5b-alembic`; the staging leg (`0000` and `upgrade head` on `aimos-db`) waits on billing (STATE 2.9) |
| S2 | `mkos-schemas` v1.0.0 (17 modules), frozen D14 | S1, EP dec. 10, T1, T61 | — | AI + FE lead approval | done: merged (PR #11); one `schemas-v1.0.0` tag after S3, on the user's go |
| S6 | `0001_core_tenancy` | S5, ADR-0006/0007 verdict, P0-U10 second half (ledger re-plan Canon PR, DATABASE.md:1758), row 0001 claimed in the S6 PR | — | team verdict | done (local): `0001_core_tenancy` on `feat/s6-core-tenancy`; the staging leg waits on billing (STATE 2.9) |
| S3 | `/api/v1` OpenAPI, TS types, `@mkos/schemas` on GitHub Packages, mock | S2 | the mock stands in for the API for the FE team | FE team (P0-U9) | built on `feat/s3-api-surface` (35 frozen ops, 501 stubs, openapi.json, `@maniacultasset/schemas` types, mock); review pending; tag and publish on the user's go |
| S7 | `0002` + `0002b` HNSW | S6 | — | — | done (local): `0002_listing_pipeline` (16 tables + `rules_revision_seq`) and `0002b_brand_chunks_hnsw` on `feat/s7-listing-pipeline`; the staging leg waits on billing (STATE 2.9) |
| S8 | `0003` + `0003b` procrastinate | S7, spike | — | — | built (local) on `feat/s8-ops-tables`: `0003_ops_tables` (14 tables, the 3 validated FKs onto 0002, read-only grants) and `0003b_procrastinate` (vendored 3.10.0 schema + `app_rw` EXECUTE); review and merge pending; spike criterion 6 and the GRANT loop on `aimos-db` wait on billing (STATE 2.9) |
| S4 | OpenTofu: import, KMS, GCS, secrets, WIF, audit, Scheduler, `deploy-staging.yml` | S1, P0-U8, EP dec. 15 | written and checked locally (`tofu fmt -check`, `tofu validate`); `tofu plan`/`apply` by the user after billing (P10) | GCP billing | not started |
| S11 | Observability spine | S8 | — | Sentry DSN (D18) | not started |
| S9 | Repository guard, isolation gate, seed | S7, S8 | — | — | built (local) on `feat/s9-repository-guard`: `TenantScoped`, the guard and `AppSession`, `unit_of_work` + GUCs, read-only `platform_scope`, `TenantScopedRepository`, `BrandChunkRepository.search`, repo rules R4-R6, `seed`, the `app_rw` isolation suite; review and merge pending |
| S14a | Rules Engine (pure function) + fixtures | S2, S7, categories | — | 2 launch categories (D18) | not started |
| S10 | Auth, RBAC, maker-checker, `0004_integrity`, `0005_rls` | S9, T7 | — | — | not started |
| S25 | Prod via OpenTofu, `deploy-prod.yml`, `v0.1.0`, rollback + PITR drills | S4, domain | — | billing, domain | not started |
| S12 | Walking skeleton over TLS | S10, S11, S4, DNS | — | billing, domain | not started |
| S17a | Pillow checks as a pure library | S2 | synthetic fixtures | images (D26) | built (local) on `feat/s17a-image-checks`: `app/services/image_checks.py` (format, corruption, EXIF orientation, file size, resolution, aspect with pack alternatives, background; pixel and JPEG-scan caps) and the `image checks are pure` import contract; review and merge pending; thresholds calibrate on real images (T110) |
| S13 | Job spine + CSV import | S12, S8, S11 | MinIO for GCS; synthetic CSVs | real CSVs (D22) | not started |
| S14b | AI seam (5 ports + fakes), `/rules/dry-run`, `/internal/admin` | S13, S3, S9, S10 | stub pipeline, FakeAdapter | — | not started |
| S15 | Marketplace accounts + OAuth, `KeyWrapper`, token refresh | S10, S13, S4, Amazon app | fake marketplace server + FakeAdapter; `LocalKmsStub` | credentials, domain | not started |
| S16 | Brand onboarding, invites, chunking + embeddings | S10, S13, model API key | none named (Open: STATE dec. 5) | model API key (D27), email (D24) | not started |
| S18 | Generation: job → `run_listing_pipeline`; `blocked` after 3 | S14b, AI drop; S16 before final gates | stub pipeline | AI drop (T104, due D29) | not started |
| S19 | Review + approval, `review_decisions`, `GET /issues` | S18 | — | — | not started |
| S20 | Adapter framework + Amazon | S15, sandbox | fake marketplace server + FakeAdapter | Amazon credentials | not started |
| S21 | Publish, per-field retry, status sync, DLQ requeue | S19; S20 before final gates | FakeAdapter first | — | not started |
| S17b | Image pipeline (minimal) | S13, S17a | MinIO for GCS | — | not started |
| S23+S24 | Analytics (4 metrics) + observability completion | S21, S11 | — | — | not started |
| S22 | Flipkart adapter, behind a flag at launch | S15, S21 | fixtures + FakeAdapter | Flipkart credentials (T78) | not started |
| S26 | Launch hardening: failure injection, load test, audits, BRD walkthrough | everything above | — | pilot brand + 2 UAT people (D30; T8) | not started |

- **"Done when":** S2–S26 come from `docs/STEPS.md` (approved 2026-10-04). A step whose untagged
  lines pass is "done (local)"; `[staging]` lines finish after billing and the domain.
- **S1 follow-ups** merged (PR #6, 2026-10-04): MinIO compose services, `editables` pin, pip-audit OSV.

### Local-first build order

Source: STATE "Start here" 2 and "Build order" (user, 2026-09-29/30); EP §10; ADR-0011 two-track
amendment (accepted 2026-10-04). It mirrors STATE "Start here" 2, which wins.

1. Done: S1 follow-ups (PR #6); pre-S2 docs (STEPS, PROCESS, ADR-0011 amendment); ADR-0004 spike
   local half.
2. Per-worktree ports (enables two tracks).
3. S2, then S3 (Track B once two tracks run).
4. S5 → S6 → S7 → S8 (Track A).
5. S9, S10 (Track A).
6. S11, S13, S14a/b, S17a.
7. S15–S24 and S26 against the stand-ins above (STATE "Start here" 2.8).
8. External waits: billing → S4 apply, thin staging deploy, S12, S25; domain → HTTPS and OAuth redirect
   URLs; Amazon/Flipkart credentials → real OAuth (S15), sandbox publish (S20/S22); AI pipeline drop
   → swap the stub for the pinned version (T104).

STATE names three stages: **A** build and prove everything locally with the AI stub (once billing is
on, deploy only the skeleton, health check + DB connection, to staging); **B** full staging and prod
hosting, and the frontend moves from the S3 mock to the live URL; **C** swap in the AI team's
pinned pipeline.

## Milestones and launch gates

Source: EP §1, §6.4, §6.5, §10; STATE "Where we are". Dates for S4, S12 and S25 now wait on GCP
billing (EP §10). 4 Nov holds only as Amazon-first with cuts C1–C10 (EP §6.6), two build tracks and same-day approvals;
full scope lands about 18–25 Nov.

| Day | Milestone |
|---|---|
| D14 Sun 4 Oct | MAJOR window 1: `schemas-v1.0.0`; launch-scope decision |
| D16 | `/api/v1` surface out; additive only until D28 |
| D18 Thu 8 Oct | staging auto-deploys from `main` (S4) |
| D25 Thu 15 Oct | walking skeleton over TLS (S12); prod, `v0.1.0`, rollback drill (S25) |
| D28 Sun 18 Oct | MAJOR window 2 |
| D29 Mon 19 Oct | foundation complete; AI drop due |
| D32 Thu 22 Oct | first AI-drafted listing on staging |
| D37–38 | first Amazon sandbox publish |
| D42 Sun 1 Nov | last MAJOR window; feature freeze; 0 open P0/P1 |
| D43 Mon 2 Nov | `v0.9.x` on prod; Go/No-Go against the 10 gates |
| D45 Wed 4 Nov | the user publishes `v1.0.0` |

Critical path (EP §6.5): S1 → S5 → S6 → S7 → S8 → S9 → S10 → S12 → S13 → S14b → S18 (+ AI drop) →
S19 → S21 → S26 → Go/No-Go, with 1–2 days of slack.

Launch gates (site L6742–6808; all 10 must pass):

| # | Gate | Built in | Target |
|---|---|---|---|
| 1 | Feature completeness | S12–S23 | D42 |
| 2 | Security + multi-tenancy | S9, S10, S15, S26 | D41 |
| 3 | Reliability + retries | S8, S13, S18, S21 | D40 |
| 4 | AI contract integrity | S2, S14b, S18 | D33 |
| 5 | Marketplace adapters | S20, S21 (+S22, cut C7) | D38 |
| 6 | Data + recovery (restore < 15 min) | S25 | D25, then D41 |
| 7 | Observability | S11, S24 | D40 |
| 8 | Performance (1,000-row CSV, 20 concurrent generations) | spike, S26 | D40 |
| 9 | UAT | S26 | D44 (Open: T8, ADR-0010) |
| 10 | Rollback | S25 | D25, then D44 |

## Phase 2 and Phase 3 (site)

Source: L4731–4849, L13283–13388; `docs/source/system-arch-content.json` `scope`;
`docs/review/REQUIREMENTS-TRACE.md`:16. Nothing here is planned yet. Open: T16 (the site tags 217
of 244 pages "mvp"; we build to the scope lock).

- **Scope-lock deferrals (L4745–4799):** catalog sync from external ERPs/PIMs; Myntra, Ajio,
  Meesho, Nykaa; Optimization Agent, learning loops, competitive scraping; generative image editing;
  confidence-based auto-publish; cross-channel sales attribution and margin modelling; Kafka;
  SAML/SSO, SOC2/ISO; self-hosted Prometheus/Grafana or Datadog; Pricing, Ads, Inventory, Returns,
  Revenue Ops, Catalog, Ranking agents ("Phase 2 / Phase 3", L4815–4849).
- **Phase 2 (L13293–13299):** listing version history with comparison UI; image pipeline
  refinements; unified analytics dashboard; sales attribution; performance monitoring; prompt
  versioning and A/B testing; webhooks.
- **Phase 3 (L13313–13317):** Optimization Agent; competitive signals; low-risk auto-publish
  (L13349); advanced model routing; AI learning loop (L13377).
- **Future:** new marketplaces as adapters (L13321); new agents (L13335).
- **Tied to v1 decisions:** `tenants.auto_publish` is reserved and off in v1 (ADR-0008; Open: T10);
  v1 analytics are operational, marketplace performance Phase 2 by default (Open: T14); the site's
  Redis + ARQ (L4779) is replaced by ADR-0004 (Open: T3). If Amazon-first is chosen, cuts C1–C10
  move items to "1.1" (EP §6.6).
