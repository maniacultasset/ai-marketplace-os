# AGENTS.md — AI Marketplace OS (Backend & Database)

The shared rulebook for every coding agent in this repo. Codex reads it directly; Claude Code loads it
through `CLAUDE.md`. It is kept short on purpose: details live in the files it points to.
**Put every project rule here, never only in one tool's private memory.**

## 1. The product and our scope

An AI-powered marketplace listing operator for Indian D2C brands. A brand connects its Amazon and
Flipkart seller accounts and uploads products once. AI then drafts the listing for each marketplace
in the brand's voice: title, bullets, description, keywords and attributes. Deterministic code
validates it, a human approves it, and only then does an adapter publish it.

```
AI proposes -> deterministic rules validate -> humans approve -> adapters execute -> performance feeds back
```

- **We own backend + database:**
  - The FastAPI API, the Postgres schema and migrations, and the workers.
  - The marketplace adapters, the Rules Engine and auth/RBAC.
  - The contracts package `packages/schemas`.
  - On the AI seam: brand-document chunking, the embedding calls, `BrandChunkRepository.search` and
    the Pillow image checks.
- **Other teams:**
  - AI team: `packages/pipeline`, LangGraph and prompts.
  - FE team: `apps/web`, Next.js.
  - Both consume our contracts, so any contract change breaks two teams.
- **The client is bootstrapped until it raises funding.** v1 must be cheap to run without weakening
  security, error handling or the path to scale.
- **Confidential, isolated client work.** This rule covers agents and dev tooling; the product's own
  Canon-mandated integrations (OpenAI, Sentry, OTel, `agent_runs`) are out of scope for it.
  - Never paste or upload project content to third-party services.
  - Never install plugins, skills or tools that phone home.
  - Never add dev-tool telemetry.
  - **Documents stay on this machine (user, 2026-09-27; reaffirmed 2026-09-29).**
    - Every document we make lives only in this folder (`docs/`, `deliverables/`). `docs/README.md`
      maps every file.
    - Render diagrams locally with `scripts/docgen/`: mermaid-cli with local Chrome, network blocked.
      Never use an online renderer such as mermaid.ink or kroki.
    - **Never** create Artifacts, Claude Docs, Google Drive, Whimsical or any other connector output
      for this project, not even previews. The only outside homes are the client's own GitHub org
      and GCP projects.
    - Delete temp copies outside this folder when the work is done.

## 2. Source of truth (resolve every conflict in this order)

**The client's spec site is the source of truth** for what we build (user, 2026-09-27):
https://system-arch.vercel.app. The snapshot lives in `docs/source/`:
- `system-arch-llms-full.txt`: all 244 pages, verified complete against the site.
- `system-arch-content.json`: per-page metadata, including `scope`: mvp, phase2, phase3 or future.

`scripts/spec-sync.sh` checks the live site for changes. It only reports; it never overwrites.

0. **The user's latest explicit instruction.** For step status and the next action, `docs/STATE.md`.
1. **The spec site**, wherever its pages agree with each other.
2. **Team-confirmed answers** in `docs/review/TEAM-QUESTIONS.md`. These resolve places where the
   site contradicts itself, is silent, or can't be built as written.
3. **Accepted ADRs** (`docs/adr/`, listed in CANON.md "Amendments by ADR"), then
   `CANON-ADDENDUM.md`, then `CANON.md`, then `ENV.md`.
   - These are our engineering rulings. They fill gaps and settle the site's internal conflicts.
   - **An ADR or Canon row that departs from a clear site statement stays provisional** until the
     team confirms it in TEAM-QUESTIONS.md.
4. **Pre-Canon drafts:** BUILD-SEQUENCE, DECISIONS, CONTRACTS, CHECKLISTS, ARCHITECTURE, BLOCKERS and
   DELEGATE-GATE. Use them for intent only, and never copy a value without checking 1–3.
5. `scaffold/`: starter files with known bugs.

**Ask, don't guess.** If a site statement is unclear, contradicts another page, conflicts with our
rulings, or looks wrong or unbuildable:
1. Add a row to `docs/review/TEAM-QUESTIONS.md`: what the site says (`path:line`), what we propose,
   the default, and the impact.
2. Ask the user. They confirm with the team.
3. Build on the recorded default only if waiting would block the step. Never deviate silently.

**Before starting a step, check the review files:**
- `docs/review/REQUIREMENTS-TRACE.md`: every checkable statement on the site (8,916, from all 13,711
  lines) with its verdict. Grep it for the step's tables, endpoints and terms, and put every ⚠️ gap,
  ❌ conflict and ❓ question that touches the step into its PLAN.md. The per-statement data is in
  `docs/trace/` (`cNN.json` register, `cNN.map.json` verdicts). After changing a verdict,
  re-run `scripts/trace/check_mapping.py` and `compile_trace.py`.
- `docs/review/TEAM-QUESTIONS.md`: T1–T102. We build each open row's default until the team answers.
- `docs/review/SPEC-TRACEABILITY.md`: every site page mapped to our plan.
- `docs/review/VISION-ALIGNMENT.md`: gaps G1–G19, conflicts C1–C10, questions Q1–Q26.
- `docs/review/SUPERSEDED.md` and `docs/review/KNOWN-ISSUES.md`.

## 3. The 10 invariants (never trade these for speed)

1. AI never publishes. Only adapters publish, and only after human approval.
2. The Rules Engine is deterministic code, never an LLM call. It is the final arbiter.
3. Human approval is mandatory, with maker-checker: whoever generated or edited a listing cannot approve it.
4. Marketplace logic stays inside adapters. The canonical listing holds no marketplace-specific fields.
5. Every query is scoped by `tenant_id`, enforced at the repository base class, with no exceptions.
6. Marketplace credentials never enter an LLM prompt, and are KMS-encrypted at rest.
7. Every AI call persists an `AgentRun` recording model, tokens, cost, duration and status.
8. There is one `ErrorReport` shape for rules, vision, model and marketplace failures alike.
9. AI self-correction is capped at 3 attempts, then the listing goes to `blocked` (lowercase).
   - The cap is enforced inside the graph; generation queue jobs use `max_attempts=1` for domain
     failures.
   - No infinite loops.
10. Mutations are idempotent, and partial retries touch only the failed field.

(Some drafts cite a 14-item invariant list, e.g. "invariant 12". Use this numbering.)

## 4. Stack

- **Application stack** (Canon, as amended by ADR-0002 to ADR-0005 on 2026-09-27):
  - Python **`==3.13.*`** (AI-team confirmation pending), FastAPI pinned exactly, Pydantic
    `>=2.13.5,<2.14`.
  - SQLAlchemy `[asyncio]>=2.0.54,<2.1` on **psycopg 3** (`postgresql+psycopg`) for both the app and
    Alembic. No asyncpg.
  - **PostgreSQL 17** with pgvector **>= 0.8.2**: HNSW index, 1536 dimensions.
  - **Job queue:** procrastinate on Postgres, with transactional enqueue. **No Redis** (gated on the
    ADR-0004 spike).
  - One `uvicorn` process per instance. No gunicorn, no slowapi (rate limits are a Postgres-backed
    dependency).
  - A `uv` workspace: one pinned uv version, `uv.lock` committed, `uv sync --frozen`.
  - PyJWT[crypto] `>=2.15` (HS256 only) and pwdlib[argon2]. Never python-jose, passlib, numpy or the
    cloud-sql-python-connector.
- **Hosting: GCP asia-south1, bootstrapped profile (ADR-0001, accepted 2026-09-27).**
  - Services:
    - Cloud Run service for the API.
    - Cloud Run **worker pool** for queue workers.
    - Cloud SQL PG17 Enterprise.
    - GCS, Secret Manager, Cloud KMS.
  - Two projects: `aimos-staging` and `aimos-prod`.
  - Provisioning happens only through reviewed OpenTofu in the step that owns it (S4/S12). **Never
    provision cloud resources by hand.**
  - Cloud-specific code lives only behind adapters: `StorageAdapter` (S3 API), KMS `KeyWrapper`,
    and secrets as env vars. Domain code never imports a cloud SDK.
  - Portability rules and cost guardrails are in ADR-0001.
- **Named non-goals:** Kubernetes, Kafka, a separate vector database, model fine-tuning, competitor
  scraping, and any marketplace beyond Amazon and Flipkart. Competitive signals and the Optimization
  Agent are Phase 3.

## 5. Canonical names most often gotten wrong (full register: CANON.md)

- **API prefix:** business routes go under `/api/v1`. The only unversioned routes are
  `/healthz /readyz /openapi.json /docs /internal/admin`.
- **Marketplaces:** the path segment is plural, `marketplaces`. The OAuth callback is
  `GET /api/v1/marketplaces/{marketplace}/callback`.
- **Tenancy:**
  - `tenant_id` everywhere, never `brand_id`. Products carry `UNIQUE (tenant_id, sku)`.
  - In HTTP requests the tenant comes only from the verified JWT `tid` claim, never from a path,
    query or body.
  - Workers take it from the `job_runs` row or `GenerationContext`.
  - The OAuth callback takes it from the server-side OAuth state.
- **Contracts package:**
  - Distribution `mkos-schemas`, import `mkos_schemas`, TypeScript package `@mkos/schemas`.
  - 17 modules (Addendum §21).
  - The generated artifacts are `packages/schemas/jsonschema/openapi.json` and
    `packages/schemas/ts/src/{api,contracts}.d.ts`. They are machine-owned: regenerate with
    `make -C packages/schemas all` and never hand-edit.
- **AI seam:**
  - Signature: `async def run_listing_pipeline(ctx: GenerationContext, deps: PipelineDeps) -> GenerationResult`.
  - `PipelineDeps` has exactly **5** ports.
  - The backend worker is the only writer of `agent_runs`.
- **Jobs:**
  - Table `job_runs`, column `status`.
  - Values `queued|running|succeeded|partial|failed|cancelled|dead_letter`, as TEXT with a CHECK
    constraint, never a Postgres ENUM.
  - Two queues: `default` and `publish` (ADR-0004).
- **Listings:** `listings.status` has 9 lowercase values. The pipeline stage is transient (12 node
  names) and is never written to `listings.status`.
- **ErrorSource:** `schema|rules_engine|vision_model|ai_model|amazon_adapter|flipkart_adapter|system`.
  Error codes come only from `mkos_schemas/codes.py`; a new code needs the Tech Lead.
- **Environment:** `APP_ENV`, never `ENV`. One `.env.example`, at the repo root. Only
  `app/core/config.py` reads the environment.
- **Migrations:** revision ids are claimed in the Canon ledger before the file is written.
  `scripts/db/0000_bootstrap.sql` is raw SQL, not an Alembic revision.

## 6. How we work

- **Build order:** `docs/drafts/BUILD-SEQUENCE.md`, S1 to S14, with no skipping ahead. Each step's
  "Done when" lines are its acceptance tests, after the fixes in SUPERSEDED and KNOWN-ISSUES.
- **The team loop.** Every step or feature goes through these roles. The role files are in
  `.claude/agents/mkos-*.md`, and both tools use them.
  1. `mkos-planner` writes `PLAN.md`.
  2. `mkos-code-planner` writes `CODE-PLAN.md`.
  3. Code and security reviewers critique both plans.
  4. **The user sets `Status: approved`** in `CODE-PLAN.md`.
  5. `mkos-implementer` builds it test-first.
  6. `mkos-e2e-tester` tests it and attacks it.
  7. `mkos-code-reviewer` and `mkos-security-reviewer` review it.
  8. `mkos-verifier` confirms or refutes each finding.
  9. Fix rounds follow, at most 3, then `REPORT.md`.
  10. **The user approves the merge.**

  All artifacts live in `docs/plans/<id>/`, so either tool can resume mid-loop.
  - Claude Code runs the loop with the `plan-step` and `build-step` workflows.
  - Codex plays each role in turn: read its role file, then write the same artifacts.
  - **Conductor:** `/drive [next|S1|S1..S3|goal]` runs the whole loop across a range of steps,
    following `.claude/skills/drive/SKILL.md`. It stops only for batched questions, the two
    approvals, a blocker, or the end of the range.
    - In Codex, when the user says "drive …", follow the same file step by step and ask the
      questions in chat. Its §5 maps each Claude-only part (workflows, AskUserQuestion) to what Codex
      does by hand.
    - Waiting for input is terminal-only, with no push notifications. There is no token cap.
      (User, 2026-09-27.)
- **Test first.** Write the failing test from "Done when" before the code, and prove every guard can
  fail. For example, remove the tenant filter and the isolation test must go red.
- **"Done" for a code change means every required check that exists yet has passed**, with fresh
  output shown. The full set is these 12 names (Addendum §12):
  - `api-lint` (ruff + `black --check`) and `api-typecheck` (mypy `--strict` on apps/api and
    packages/schemas; pipeline non-strict).
  - `api-tests` (alembic upgrade head → `alembic check` → downgrade base → upgrade head, then pytest)
    and `tenant-isolation`.
  - `contracts`, `schemas-drift`, `web-lint`, `web-typecheck`, `commit-lint`, `bootstrap`,
    `lint-imports` and `gitleaks`.
  - State what was skipped and why. Docs-only changes have no gates: say "docs-only, no gates run".
- **Who approves:**
  - The user acts as Tech Lead unless `STATE.md` names someone else.
  - The user approves each step's plan before coding, and every merge to `main`.
  - An approval counts only when the user gives it explicitly, either by editing the file or by
    answering an approval question in chat. The agent then records it as `Status: approved (user
    via <tool>, <date>)`. An agent never approves on its own.
  - Agents write ADRs (`docs/adr/NNNN-title.md`, template `0000`) only as `Status: proposed`. Only
    the user sets `accepted`, and STATE.md records it.
  - Canon row edits, migration-ledger claims and new `ErrorCode`s need the user's explicit OK before
    any code.
- **Contracts:**
  - Every change to `packages/schemas`, MINOR or MAJOR, needs the Tech Lead, the AI lead and the FE
    lead. An AI-seam change is also a PR against `CANON.md`.
  - MINOR is an additive optional field and can land any day.
  - MAJOR (rename, removal or type change) can land only in the Day-14, Day-28 or Day-42 windows.
- **Changes are additive only.** Never remove an endpoint or a contract field without a migration
  plan. Never weaken tenant isolation to make a test pass.
- **Git** (once this folder is a repo; see §7):
  - Branch names match `^(feat|fix|chore|docs|refactor|test|perf)/[a-z0-9._-]+$`.
  - The PR title matches `^(feat|fix|chore|docs|refactor|test|perf)(\([a-z0-9-]+\))?: .+`, so no
    `ci:`, `build:` or `!`.
  - Squash merge only. Never push or merge without the user.

## 7. Session protocol (Claude Code and Codex hand off to each other)

- **Start:**
  1. Read `docs/STATE.md`.
  2. If STATE says this folder is a git repo, check `git status` and the branch. If it is not:
     - Skip every git step.
     - Never run `git init`, `gh repo create` or a push yourself.
     - Use `no-git` as the branch.
  3. If `Active:` is not `none`:
     - If it is older than 12 hours, or it is yours from a crashed session, ask the user whether to
       clear it.
     - Otherwise stop and ask the user.
  4. If your loaded instructions say GCP is "decided — do not relitigate", you are running the
     retired `CLAUDE-v0`. Ask the user to restart the session.
  5. If the `docs/source/` snapshot is more than 3 days old (STATE "Spec snapshot"), run
     `scripts/spec-sync.sh` and tell the user about any changed pages before planning.
- **Before your first edit:** set `**Active:** <claude-code|codex> · <branch|no-git> · since
  <YYYY-MM-DD HH:MM>` in STATE.md.
- **End:**
  1. Update STATE.md: `Last updated`, what was done, what's next, open questions, `Branch:`, and
     `**Active:** none`.
  2. Without git, list the files you touched in the session log.
  3. Commit only in a git repo, and only if the user allows it.
- **Workflow runs and role agents:**
  - The session that launches `plan-step`, `build-step` or `/drive` owns this protocol for the run.
    Codex does the same when it plays the roles.
  - It sets `Active: … · <plan-step|build-step> <id>` before the run, and logs the run and clears
    `Active` afterwards.
  - **Role agents skip Start/End and never edit `docs/STATE.md`.** Their facts come from
    `docs/plans/<id>/CONTEXT.md`, and the loop state is in `LOOP.md` (see `docs/plans/README.md`).
- **Truth rules:**
  - Files beat memory.
  - Every claim about this project cites a file (`path:line` or a Canon row).
  - If the docs don't specify something, say "not specified" and ask. Never invent endpoints,
    columns, env vars, error codes or versions.
- **Reviews in Codex:** Codex has no subagents, so run the plan and code reviews one after another
  in the same session. Re-check each finding against the cited line, and log "single-agent review".

## 8. Token discipline

- **Don't read the big docs whole; grep for the section you need.** Sizes:
  - `CHECKLISTS` 188 KB, `CONTRACTS` 55 KB, `CANON` 49 KB, `BUILD-SEQUENCE` 46 KB (grep `^## S<n>`).
  - `CANON-ADDENDUM` 42 KB, `BLOCKERS` 35 KB, `DECISIONS` 24 KB, `ARCHITECTURE` 20 KB.
  - `docs/architecture/DATABASE.md` 250 KB (the schema: tables, columns, RLS, migration ledger §8.2)
    and `BACKEND-ARCHITECTURE.md` 254 KB. Run `grep -n '^#'` for the section index first.
  - `REQUIREMENTS-TRACE` 215 KB, `SPEC-TRACEABILITY` 74 KB, `TEAM-QUESTIONS` 60 KB. Query
    `docs/trace/*.json` (3 MB) with python or jq, never by reading them.
  - `docs/source/system-arch-llms-full.txt`, 388 KB. This is the client's spec site
    (system-arch.vercel.app), with pages starting at `^# `: the Founder BRD, the 45-day plan and
    specs [1]–[221]. How it maps to the Canon is in `docs/review/VISION-ALIGNMENT.md`.
- **Folder rules:** a folder may have its own `AGENTS.md`, for example `apps/api/AGENTS.md`. It adds
  to this file and wins inside that folder.
  - Before editing a file, read every `AGENTS.md` between the repo root and that file's folder that
    isn't already in your context. Codex auto-loads only the files down to its launch folder.
  - Every nested `AGENTS.md` needs a sibling `CLAUDE.md` whose only line is `@AGENTS.md`, or Claude
    Code never sees it.
  - Never create `AGENTS.override.md`.
- **Keep this file under 32 KiB.** Codex stops reading project docs at 32 KiB by default
  (`project_doc_max_bytes`), so detail goes into the files this one points to.
- **Link to the Canon; don't restate it.**
- **Durable project facts** go here, in `docs/STATE.md` or in an ADR.
- **Documents for humans vs agents (user, 2026-09-27):**
  - Team-facing documents are `.docx` in `deliverables/`, with diagrams rendered as images.
  - They are generated from `.md` sources in `docs/`, which the agents read.
  - Edit the `.md`, then regenerate the `.docx`. Never edit the `.docx` alone.
- **Not agent inputs:** `deliverables/` (the kickoff .docx) and `docs/archive/` (retired files).
  Ignore them unless the user asks.
