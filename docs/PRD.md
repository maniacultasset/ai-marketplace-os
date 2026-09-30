# PRD: AI Marketplace OS v1

**Purpose:** a single entry point that says what v1 must do, for whom, and what it leaves out.

**Read with:** `docs/source/system-arch-llms-full.txt` (the client spec), `docs/STATE.md`,
`docs/EXECUTION-PLAN.md`, `docs/architecture/BACKEND-ARCHITECTURE.md`, `docs/architecture/DATABASE.md`,
`docs/review/TEAM-QUESTIONS.md`, `docs/adr/`.

This file is a summary. The documents above remain the single source of truth. Where they disagree
with this file, they win, in the order given in `docs/README.md`. `L<n>` means a line of
`docs/source/system-arch-llms-full.txt`, and `Open: T<n>` means a TEAM-QUESTIONS row the team has
not yet answered; until they do, its default applies.

## 1. Problem

For every product and every marketplace, a brand has to write the title, bullets, description,
keywords and attributes by hand, then reformat them for that platform's rules (L7077). Those rules
change over time (L29). A listing counts as done only when the marketplace confirms it is live, not
when it is submitted (L183–185). The product drafts the content in the brand's voice, checks it with
deterministic rules, has a person approve it and publishes it through adapters (L7065–7109).
Source: `docs/source/system-arch-llms-full.txt`.

## 2. Users and roles

- **Who:** Indian D2C brands selling on Amazon.in and Flipkart (BACKEND-ARCHITECTURE.md:137–138).
  The primary niche is apparel (L43–45). The spec lists three user types: brand owners, listing
  operators or catalogue teams, and reviewers (L7409–7433).
- **Roles:** owner, operator, reviewer, admin (L7445–7475). The creator of a listing never approves it
  (L7477–7483). Our v1 role defaults are in BACKEND-ARCHITECTURE.md §9.2. Open: T88 (can an admin
  approve?), T15 (brands with only one person).
- **Tenancy:** a tenant is one brand workspace (Open: T31). Users are global and hold a membership in
  each brand, and in v1 people join only by invitation (ADR-0006, proposed; Open: T23).
- **v1 scale:** 5–30 brands, about 50k products, imports of up to 1k rows, and 20 generations
  running at once (STATE "Decided", "Hosting inputs").

## 3. Safety chain

**AI proposes → deterministic rules validate → humans approve → adapters execute → performance feeds
back** (L4713–4715). There are 10 invariants that the team never trades for speed (L4692–4707):

1. AI never publishes.
2. The rules engine is deterministic code.
3. A human must approve, and cannot approve their own work.
4. Marketplace-specific logic stays inside the adapters.
5. Every query is scoped by `tenant_id`.
6. Marketplace credentials never enter a prompt, and they are encrypted with KMS.
7. Every AI call leaves an `AgentRun` record.
8. All failures use one `ErrorReport` shape.
9. A listing gets at most 3 correction attempts.
10. Mutations are idempotent, and a retry resends only the failed fields.

How each invariant is enforced: BACKEND-ARCHITECTURE.md §2.3.

## 4. v1 definition of done

v1 is the spec's Day-45 Definition of Done (STATE "Decided", "v1 expectations confirmed"): a real brand user completes the whole
journey in production with no engineer stepping in (L4679–4690).

| # | Journey step (L4683–4690) | Built in |
|---|---|---|
| 1 | Sign in with a JWT, choose a brand, see only that brand's data | S9, S10 |
| 2 | Connect Amazon and/or Flipkart through OAuth, with the tokens encrypted at rest | S15 |
| 3 | Import products by CSV or manual entry, with validation and image upload to GCS | S13, S17b |
| 4 | Generate title, bullets, description, keywords and attributes in the brand's voice (pgvector RAG) | S16, S18 |
| 5 | See rule and image failures as one structured error | S14a, S17b |
| 6 | Watch the system fix field errors itself (at most 3 retries), or stop cleanly and wait for a person | S18 |
| 7 | Review each marketplace side by side, edit, then approve or reject under maker-checker, with an audit trail | S19 |
| 8 | Publish with a result for each field, and see operational analytics | S20–S23 |

Step numbers come from EXECUTION-PLAN.md §6.2–§6.3.

## 5. v1 features by area and who builds them

Scope is the Phase-1 scope lock (L4731–4799; STATE "v1 expectations confirmed"). Anything not listed there goes to the
backlog (L4805).

| Area | Ships in v1 (site) | Our part in this repo | Where we differ, or what is open |
|---|---|---|---|
| Product input | CSV, manual entry, normalisation, GCS images (L4743) | import and product APIs, CSV job, signed upload | Open: T67 (does an import start generation?), T75 |
| Marketplaces | Amazon SP-API and Flipkart adapters (L4749) | both adapters, OAuth, KMS token storage | Open: T9, T77, T78 |
| AI generation | Content, Keyword and Attribute agents, brand-voice RAG, structured JSON (L4755) | the generation job, the 5 seam ports, document chunking, embeddings, `BrandChunkRepository.search`, sole writer of `agent_runs` | the agents and prompts belong to the AI team (BACKEND-ARCHITECTURE.md §6) |
| Images | GCS upload, Pillow checks, vision checks, `ImageReport` (L4761) | Pillow checks, the vision call, stored reports | the vision prompt belongs to the AI team (BACKEND-ARCHITECTURE.md:1540) |
| Control plane | schema validation, rules engine, `ErrorReport`, correction loop of at most 3, maker-checker (L4767) | all of it except the correction loop, which runs inside the pipeline | Open: T12, T13, T40 |
| Analytics | volume, approval rate, validation pass rate, publish success rate (L4773) | `GET /analytics` | Open: T14 |
| Reliability | Redis + ARQ queue, backoff, re-running failed jobs, correlation IDs (L4779) | a queue in Postgres (procrastinate), with no Redis | ADR-0004 (depends on a spike); Open: T3 |
| Security | JWT, 4-role RBAC, tenant-scoped repositories, KMS-wrapped tokens, audit log (L4785) | all of it; FastAPI issues the JWT | Open: T7, T23 |
| Observability | Sentry, OpenTelemetry, Cloud Trace, `AgentRun` cost, adapter logs (L4791) | all of it | none |
| Agent roster | Content, Keyword, Attribute, Image and Brand RAG agents (L4797) | the ports only | AI team repository (Open: T104) |

**Who builds what (ADR-0012; Open: T103):**
- **This repository (backend and database):** the API, the schema and migrations, the workers, the
  adapters, the rules engine, auth and RBAC, the contracts in `packages/schemas`, and the infra.
- **AI team, in its own repository:** the `mkos-pipeline` package. Our worker installs it at a pinned
  tag, and our CI runs a stub in its place.
- **Frontend team, in its own repository:** the dashboard. It installs `@mkos/schemas` at a pinned
  version.

Contract changes also need the AI and frontend leads' approval. Breaking (MAJOR) changes land only in
the Day-14, Day-28 and Day-42 windows (ADR-0012, Decision 5).

## 6. Non-goals

- **Spec non-goals (L4807–4813):** no model training or fine-tuning, no Kubernetes, no dedicated
  vector database, no auto-publishing and no scraping.
- **Deferred column of the scope lock (L4745–4799):** ERP and PIM catalogue sync, Myntra, Ajio, Meesho
  and Nykaa, generative image editing, confidence-based auto-publish, sales attribution and margin
  modelling, Kafka, SAML/SSO, SOC2/ISO certification, and a self-hosted metrics stack.
- **Not a v1 objective in the BRD (L187–217):** order management, inventory and warehouse
  management, advertising, standalone pricing, returns and reconciliation, customer-service
  automation, and a full business operating system.
- **This repository:** no frontend code and no AI pipeline code (ADR-0012).

## 7. Deferred to Phase 2, Phase 3 and later

- **Phase 2 (L13291–13299):** listing version history with a comparison view, image pipeline
  refinements, a unified analytics dashboard, sales attribution, performance monitoring, prompt
  versioning and A/B tests, and webhooks.
- **Phase 3 (L13311–13317):** the Optimization Agent, competitive signals, low-risk auto-publish, and
  model-routing optimisation. The AI learning loop (L13377–13385) is also Phase 3. v1 reserves
  `auto_publish` and keeps it off (ADR-0008, proposed; Open: T10).
- **Future (L13321–13347):** more marketplace adapters and new agents (Pricing, Ads, Inventory,
  Returns, Revenue Operations). The scope lock defers the Pricing, Ads, Inventory, Returns, Revenue
  Ops, Catalog, Ranking and Optimization agents to "Phase 2 / Phase 3" (L4815–4849).
- **Deferred by our defaults, pending the client:** marketplace performance data (Open: T14),
  catalogue sync (Open: T75), social login (Open: T97), and performance weighting and learning
  from edits or rejections (v1 still stores approved listings as brand examples unless cut C5
  drops them; Open: T93).

## 8. Success criteria and launch gates

- **"Listed" means live:** a listing counts as listed only when the marketplace confirms it
  (L183–185). Until then we show "Submitted" (Open: T13).
- **Metrics:** v1 ships the four operational metrics (L4773). The ten product metrics in the BRD
  (L4520–4564) are Open: T14 (cut C1). The cost-per-listing target is Open: T102.
- **Testing:** automated gates only, with no manual UAT (ADR-0010, proposed; Open: T8).
- **Launch gates:** all 10 must pass before production traffic (L6740–6808). The evidence and target
  day for each are in EXECUTION-PLAN.md §6.4.

| Gate | Pass condition (site) | Target |
|---|---|---|
| 1 Feature completeness | every left-column feature works end to end on staging | D42 |
| 2 Security and tenancy | isolation, RBAC, maker-checker and KMS pass an audit | D41 |
| 3 Reliability | retries capped at 3, idempotency keys, failed jobs can be re-run | D40 |
| 4 AI contract | JSON output is validated, `AgentRun` cost is recorded, nothing bypasses the rules | D33 |
| 5 Adapters | Amazon and Flipkart return a result for each field | D38 (Open: T78) |
| 6 Data and recovery | a backup restore finishes in under 15 minutes | D25, then D41 (Open: T87) |
| 7 Observability | Sentry alerts, trace correlation and cost dashboards are live | D40 |
| 8 Performance | a 1,000-row CSV and 20 concurrent generations run without starving the queue | D40 |
| 9 UAT | a realistic product set completes the journey | D44 (Open: T8) |
| 10 Rollback | rollback is automated and tested | D25, then D44 |

**Milestones:** feature freeze and the last MAJOR window on D42 (Sun 1 Nov), Go/No-Go on D43 (Mon 2
Nov), and `v1.0.0` on D45 (Wed 4 Nov) (EXECUTION-PLAN.md §6.4).

## 9. Open product questions

**The launch-scope decision is due Sun 4 Oct (D14).** The choice is between two options:
- an Amazon-first launch on 4 Nov, with cuts C1–C10;
- full scope, landing around 18–25 Nov.

The client decides (EXECUTION-PLAN.md §2 decision 9, §6.5 and §6.6; STATE "v1 expectations
confirmed"). None of the
cuts touches the 10 invariants.

| Row | Question | Default we build |
|---|---|---|
| T9 | Which marketplaces? | Amazon.in and Flipkart only |
| T10 | Autonomous submission (BRD) or approval? | approval in v1, with `auto_publish` off |
| T13 | Is "published" the same as "live"? | "Submitted" until the marketplace confirms |
| T14 | Analytics in v1 | operational metrics; performance data in Phase 2 |
| T15 | Brands with only one person | a second reviewer is required |
| T16 | Scope size | the scope lock, not the 217 pages tagged "mvp" |
| T42 | Batch approve and batch publish | batch-shaped contracts (cut C4 would drop batch actions) |
| T67 | Does an import start generation? | no; the user picks rows and marketplaces |
| T77, T78 | Marketplace developer applications; Flipkart sandbox | the client files now; FakeAdapter until Flipkart confirms a sandbox |
| T93 | Memory of approved listings in v1 | store it; learning stays in Phase 3 (cut C5) |
| T103, T104 | Where the code lives | backend-only repository; the pipeline pinned from the AI team's repository |

Proposed ADRs 0006–0008 (identity, apparel variant families, human approval) are with the team
(`docs/adr/README.md`). Client inputs with due dates (domain, launch categories, credentials, sample
data) are listed in EXECUTION-PLAN.md §3.2 P0-C5. The full list of rows is in
`docs/review/TEAM-QUESTIONS.md`.
