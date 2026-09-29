# STATE — read first, update last

_Last updated: 2026-09-29 (Tue) by Claude Code. Either Codex or Claude Code can pick up from here. Docs map: `docs/README.md`. Plan: `docs/EXECUTION-PLAN.md`._
**Active:** claude-code · main · since 2026-09-29 20:41 · drive S1 (planning; build waits for Docker Desktop)
**Branch:** main
**Git repo:** yes. Private `maniacultasset/ai-marketplace-os` (https://github.com/maniacultasset/ai-marketplace-os); first commit `be02277` on 2026-09-29. From here on, every change goes branch → PR → squash merge, and the user approves each merge.
**Hosting ADR:** accepted ADR-0001 (GCP asia-south1, bootstrapped profile; 2026-09-27)
**Tech Lead:** the user (to be confirmed)

## Where we are

- **Sprint:** Mon 21 Sep → Wed 4 Nov 2026 (45 days). Day N = today's date − 20 Sep 2026, so Sat
  26 Sep = Day 6.
- **Phase:** pre-S1. No code yet; the **docs foundation is complete** (2026-09-28): stack and hosting
  ADRs, DB and backend architecture, Word docs for the team, and a line-by-line trace of the client
  spec against our plan.
- **Build location:** this folder becomes the monorepo root. The user decided this on 2026-09-26.
  - This voids `scaffold/README.md`'s instruction to copy the scaffold into a separate repo. **Never
    run its `cp -R scaffold/.` here:** it overwrites the root README.
  - Files move one by one during S1, after the edits in `docs/review/SUPERSEDED.md` and the bug
    fixes in `docs/review/KNOWN-ISSUES.md`.

## Decided (2026-09-26/27; ADRs 0001–0005 accepted, 0006–0011 written as proposed)

- **Agent loop, per step:**
  1. Context pack.
  2. Plan, then a critique of the plan.
  3. **The user approves the plan.**
  4. Tests first, then the implementation.
  5. Gates.
  6. Reviews (correctness/Canon fit, security, test quality), each finding re-checked.
  7. Fix rounds, capped at 3, then `blocked` and the user is asked.
  8. **The user approves the merge.**
  - In Claude Code the reviews run in parallel. In Codex they run one after another (AGENTS.md §7).
- **Claude Code and Codex are interchangeable.** Handoff goes through this file.
- **Third-party skills evaluated, none installed:** find-skills, superpowers, claude-mem, impeccable
  and task-observer. The only thing taken is test-first discipline, in AGENTS.md §6.
- **Remote Control** is off for this project.
- **Hosting inputs (user, 2026-09-27):**
  - No cloud is set up yet.
  - No India-only data requirement; we scale globally later, but the first region stays near India.
  - v1 scale: 5–30 brands, about 50k products, 1k-row imports, 20 concurrent generations.
  - Budget: the client applies for startup credits, so year 1 is covered; year-2 cost without
    credits must stay bearable.
- **Release flow (user, 2026-09-27):**
  - Tagged releases: a merge to `main` auto-deploys to staging.
  - Prod deploys only a SemVer tag (`vX.Y.Z`) after the user approves, with a changelog.
  - Rollback means redeploying the previous tag.
- **Environments (user, 2026-09-27):** A only: local (Docker) + staging (cloud, auto-deploys from
  `main`) + prod (cloud, tagged releases). No cloud dev tier.
- **Testing (user, 2026-09-27):** automated only, with no manual UAT. The backend team owns merges
  and prod rollouts, so the automated suite must catch small regressions. Written up as ADR-0010
  (proposed; it departs from the site's UAT gate, T8).
  - **4 gates:** PR (at most ~10 min), nightly, staging deploy, prod deploy.
  - **Test toolset** (proposed in chat 2026-09-27; S1 adds them as pinned dev dependencies):
    Hypothesis, Schemathesis, squawk, mutmut, pytest-randomly, pytest-socket, pip-audit, plus the
    Canon gates.
  - **Quality bar:** strict on the core modules (rules engine, tenant guard, maker-checker,
    idempotency, publish retry): 90% branch coverage plus a nightly mutation score of at least 80%.
    Everything else follows the Canon coverage ramp.
  - **Prod deploy:** zero-traffic revision → smoke test on its private URL → shift traffic → watch
    10–15 min → auto-rollback on errors. **The host must support revision traffic control.**
- **Product decisions that shape the schema (user, 2026-09-27; each needs an ADR, and some need a Canon edit):**
  - **Publishing:** human approval (maker-checker) in v1. The schema reserves a per-tenant
    `auto_publish` setting, **off in v1**. Invariant 3 is unchanged for v1. This resolves open
    decision 6 for v1; the client still has to confirm the long-term position.
  - **Apparel with variant families:**
    - `products.parent_product_id`, `variant_attributes` and `variation_theme`.
    - A `parent_sku` column in the CSV.
    - Adapters publish parent/child families.
  - **Multi-brand logins, designed now:**
    - Global users with a unique `lower(email)`, plus a `tenant_memberships` table
      (user × tenant × role).
    - The JWT `tid` is the active tenant; switching tenant issues a new token.
    - The maker-checker FKs point at memberships.
    - **This changes Canon 0001** (`users` unique per tenant) and fixes the login-ambiguity defect.
  - **Invite-only onboarding in v1:**
    - `tenants.status/plan/trial_ends_at` are added now, so self-serve later is additive.
    - This needs a transactional email provider (to be picked with hosting).
- **Source of truth (user, 2026-09-27): the client spec site** (https://system-arch.vercel.app),
  following AGENTS.md §2.
  - **Spec snapshot:** 2026-09-27, `updatedAt` 2026-09-26T11:40:32Z, verified complete (244 pages).
  - Anything unclear or deviating goes to `docs/review/TEAM-QUESTIONS.md`, which the user takes to
    the team. Our ADRs that depart from the site are provisional until the team confirms them.
- **Conductor (user, 2026-09-27):**
  - `/drive` runs the team loop across step ranges (e.g. `S1..S3`) without asking between steps.
  - It stops only for batched questions, plan and merge approvals, blockers, or the end of the range.
  - Terminal-only prompts: no push notifications.
  - No token cap; cost is logged per step.
  - A SessionStart hook (`.claude/hooks/session-status.sh`) shows this file's status at every
    session start.
- **Stack (user, 2026-09-27; ADRs 0002–0005 accepted in chat and recorded in CANON.md "Amendments by
  ADR"):**
  - Python + FastAPI.
  - **Python 3.13.** The AI team has to confirm; they also haven't confirmed Python + LangGraph yet.
  - **PostgreSQL 17** with pgvector ≥ 0.8.2.
  - **Job queue in Postgres (procrastinate), no Redis, psycopg 3 driver.** Gated on the 1-day spike
    in ADR-0004.
  - **Hygiene package:** no slowapi, one uvicorn per instance, new security floors, pinned codegen,
    pip-audit + Renovate.
- **Docs and handoff (user, 2026-09-27/28):**
  - Documents stay on this machine (AGENTS.md §1). The Word docs build offline with
    `scripts/docgen/build.sh`.
  - Proposed ADRs 0006–0011 are marked "⚑ DECISION NEEDED (team)": the team decides, and we build
    them as written until then. In the Word docs, open decisions are highlighted amber.
  - **No more big doc workflows:** doc gaps close step by step, inside each build step's plan. Codex
    takes over the foundation on 2026-09-28.
- **GCP bootstrap by hand, now (user, 2026-09-29):** the user wants a cloud DB from day one and fast go-live.
  - The user runs the one-time `gcloud` setup under access granted from the client's Google account:
    - projects `aimos-staging` and `aimos-prod`;
    - APIs enabled;
    - Cloud SQL PG17 `aimos-db` (`db-custom-1-3840`, asia-south1, PITR) with database `aimos` and user
      `app`, plus `CREATE EXTENSION vector`;
    - an Artifact Registry repo `aimos`.
  - **Superseded 2026-09-29:** development and tests use a **local PostgreSQL 17 in Docker** on this
    Mac; Cloud SQL is for **staging and prod** only (user). `cloud-sql-proxy` is for admin access to
    staging.
  - This departs from AGENTS.md §4 ("only reviewed OpenTofu"). At **S4, `tofu import` these resources**
    (never create duplicates), then change them only through code.
  - Automated tests still use local Docker Postgres.
- **GCP account (2026-09-29):** `maniacultasset@gmail.com`, a personal account with no organisation.
  - Projects **`aimos-staging`** (697366398083) and **`aimos-prod`** (1039991710726) were created by
    Claude with `gcloud`; there is also an unused auto-created "My First Project".
  - **No billing account yet.** The user or client creates one in the console, then we link it.
  - Local tools in `~/.local/bin`: `gcloud` 587 (usage reporting off) and `cloud-sql-proxy` 2.26.0.
- **Client GitHub:** org `maniacultasset`.
  - Free plan, 0 repos, 2 members (`codekid69` = the user, `Fosh-AI-official`).
  - **Members may create private repos**, so we can create `maniacultasset/ai-marketplace-os` ourselves.
  - On Free, private repos get **no rulesets, required checks, CODEOWNERS enforcement or
    environments**. The plan recommends GitHub Team (EXECUTION-PLAN decision 1).
  - The `gh` token on this Mac is `codekid69`'s OAuth token. The plan recommends a bot account for
    agents (decision 2).
- **Setup decisions (user, 2026-09-29; EXECUTION-PLAN §2):**
  - **GitHub stays on Free for now**, so nothing is enforced: PR checks run as discipline only.
    Upgrade to Team before the first prod release.
  - **Agents push as `codekid69` for now**; move to a bot account before any prod credential exists.
  - **Claude ran `git init`, the first commit, `gh repo create` (private) and the first push**, a
    one-time exception to AGENTS.md §7 authorised by the user.
  - Repo settings: squash-only, delete branch on merge, wiki/projects off, dependency vulnerability
    alerts on, Dependabot auto-fix PRs off (Renovate per decision 17).
  - **Two build tracks: yes.** Claude drafts the ADR-0011 + AGENTS.md §7 amendment for the user's
    approval before S2.
  - Recommended defaults applied: trunk-based branching (decision 3); no `gcloud`-created `app`
    database user (decision 6); generated `.docx` files not in git (decision 19, `.gitignore`).
- **Repository scope (user, 2026-09-29; ADR-0012):**
  - **Backend-only repo.** No `apps/web`; the frontend team has its own repository.
  - Required checks drop from 12 to 10: `web-lint` and `web-typecheck` leave.
  - Contracts are published: `@mkos/schemas` goes to GitHub Packages (npm, user's choice); Python
    consumers pin `mkos-schemas` by git tag.
  - **AI pipeline in the AI team's own repo, pinned by git tag, with a stub in our CI.** This is the
    recommended default, which the user asked for; the AI team confirms in T104. T103 records the
    departure from the site's monorepo.
  - Role: our team owns the backend and database end to end (schema, APIs, workers, adapters, rules,
    security, cloud), and designs the APIs.
- **Client repo carries no AI branding (user, 2026-09-29):** PR #1 was merged as `4b75388`.
  - `.claude/`, `AGENTS.md`, `CLAUDE.md` and `.codex/` stay local through `.git/info/exclude`.
  - No AI co-author lines in commits or PRs.
  - README and description rewritten.
  - The first two commits (`be02277`, `ef0f3be`) still contain the tooling files in history. Rewriting
    them needs a force-push, which the tool's safety guard blocked; the user decides whether to do it.
- **Hosting research done:** `docs/research/hosting-2026-09.md`.
  - Recommended: **AWS Mumbai + Activate credits**, plus Microsoft credits for Azure OpenAI tokens.
  - **Conditional on** the client being incorporated and pursuing DPIIT, and on team AWS skills.
  - **Decided: ADR-0001, GCP asia-south1.** The user delegated the choice: incorporation is
    unknown, the user knows GCP a little, and year-2 cost is equal to AWS. Estimate about
    $195–205/mo (prod + staging) before GST. Credits: $300 trial (90 days) + $2k Start tier
    (discretionary). Azure OpenAI credits are to be proposed to the AI team.

## Open decisions

| # | Decision | Owner | Blocks |
|---|---|---|---|
| 1 | ~~Hosting~~ **Decided:** ADR-0001, GCP asia-south1. | — | — |
| 2 | ~~App stack~~ **Decided:** ADR-0002 to ADR-0005 (Python 3.13, PG17, procrastinate/no Redis, hygiene). | — | — |
| 3 | ~~Docker runtime~~ **Decided 2026-09-27:** Docker Desktop (free under 250 staff and $10M revenue). **The user installs it** from docker.com (Apple silicon build). | user | S1, the ADR-0004 spike |
| 4 | `git init` here, then the private repo `maniacultasset/ai-marketplace-os` (members can create it). GitHub Team plan, bot account and branch model: EXECUTION-PLAN decisions 1–4. | user / client | S1, safe handoffs. Codex finds the root AGENTS.md only through `.git`. |
| 5 | Status of client items is **unknown**: incorporation/GST/DPIIT, apex domain, Amazon and Flipkart applications (due 23 Sep), cloud billing account and credit applications (ADR-0001 plan), OpenAI key, launch categories, UAT users. By its own wording, Addendum interim rule (3), the fallback apex, has triggered. **Choosing an apex is the user's call; agents never pick or register one.** | user → client | S4, S10, S12, adapters |
| 6 | Vision: v1 is human approval with `auto_publish` reserved (decided 2026-09-27). The **client** still has to confirm, since the Founder BRD says "autonomous submission" and uses a 14-status model. | client | long-term roadmap |
| 7 | Was the kickoff pack sent to the client? Its B1 blocks carry the wrong KMS ring and `/v1/marketplace` redirect URIs. Were they used to file with Amazon or Flipkart? | user | marketplace filing |
| 8 | ~~Multi-brand logins~~ **Decided 2026-09-27:** memberships, designed now. | — | — |
| 9 | **AI team:** confirm Python + LangGraph in-process, **Python 3.13** (ADR-0002), and **Azure OpenAI** for Microsoft credits (the user will propose it). | user → AI team | S2 (the seam), S13 |
| 10 | **Schema source for S5–S10:** accept `docs/architecture/DATABASE.md` rev 3 as the schema source, building its `[Proposed]` items as written until reviewed. `mkos-planner` uses it only once STATE marks it accepted. Until then it falls back to the CANON.md migration ledger, which predates the 2026-09-27 decisions. | user (Tech Lead) | S5 onward |

## Next actions

**Start here (Codex or Claude Code, from 2026-09-29):**
0. **Docs were reorganised on 2026-09-29.** Read `docs/README.md` for the map (old `docs/context/…` →
   `docs/{canon,drafts,review,trace}/`). The plan from setup to launch is `docs/EXECUTION-PLAN.md`:
   read its §1 and §2 decisions first.
1. **The user, before any code:**
   - Install Docker Desktop.
   - Run `git init` here (a `.gitignore` is ready) and commit the docs baseline.
   - Any GitHub repo must be **private**: this is confidential client work.
2. **Read:**
   - `AGENTS.md`, then this file.
   - `BUILD-SEQUENCE.md` `## S1`, then `SUPERSEDED.md` and `KNOWN-ISSUES.md`.
   - Grep `docs/review/REQUIREMENTS-TRACE.md` for the step's terms.
3. **ADR-0004 spike** (1 day, needs Docker): procrastinate transactional enqueue, the 8 pass criteria.
   The result settles T3.
4. **S1 through the team loop:**
   - **Codex:** follow `.claude/skills/drive/SKILL.md`; its §5 maps each Claude-only part.
   - **Claude Code:** `/drive S1..S3`.
   - Each PLAN.md lists the ⚠️ gaps, ❌ conflicts and ❓ questions of REQUIREMENTS-TRACE.md that touch
     the step. This replaces the build-readiness audit, which the user stopped on 2026-09-28.
5. **Close doc gaps step by step** (user, 2026-09-28): 285 backend/DB, 142 shared and 103 ops gaps are
   listed in REQUIREMENTS-TRACE.md.
   - Fix `DATABASE.md` / `BACKEND-ARCHITECTURE.md` in the step that needs the fix.
   - Update the verdicts in `req-trace/cNN.map.json`, then run `scripts/trace/check_mapping.py` and
     `compile_trace.py`.
   - Rebuild the Word docs with `scripts/docgen/build.sh`.
6. **The user takes to the team:**
   - TEAM-QUESTIONS T1–T102 (95 open).
   - ADRs 0006–0011 (⚑ proposed).
   - The Word docs' first page lists them all.
   - Also open: decisions 4, 5, 7, 9 and **10 (accept DATABASE.md as the schema source before S5)** below.

**Done (2026-09-26 → 28):**
- **Research:**
  - Context brief (`KNOWN-ISSUES.md`, `SUPERSEDED.md`).
  - Competitor analysis (`docs/research/catalogus.md`).
  - Stack and hosting research (`docs/research/`).
- **ADRs:**
  - 0001–0005 accepted and recorded in CANON.md "Amendments by ADR".
  - 0006–0011 proposed.
- **Tooling:** the agent team (`.claude/agents/mkos-*.md`), `/drive`, the SessionStart hook and
  `scripts/spec-sync.sh`.
- **Architecture:**
  - `docs/architecture/DATABASE.md` rev 3: site-aligned, 24 diagrams.
  - `docs/architecture/BACKEND-ARCHITECTURE.md`: 42 diagrams.
- **Word docs** in `deliverables/`, built by `scripts/docgen/build.sh`:
  - "Backend & DB Architecture": 63 diagrams; chapter 7 is generated from DATABASE.md.
  - "Database Design (reference)".
  - Both open with a "Decisions needed" page.
- **Spec trace:**
  - Snapshot verified complete; the live site is unchanged (`updatedAt` 2026-09-26T11:40:32Z, checked
    2026-09-27).
  - Page level: `SPEC-TRACEABILITY.md`, verified twice.
  - Line level: `docs/trace/` holds 11,167 entries (8,916 checkable) and covers 100% of
    the 13,711 lines (`scripts/trace/check_coverage.py`).
  - Every statement has a verified verdict (`check_mapping.py`).
  - Summary in `REQUIREMENTS-TRACE.md`: 78% covered (6,063 as written, 914 changed on purpose), 550
    gaps, 67 conflicts, 273 questions. New TEAM-QUESTIONS rows T73–T102.

## Environment facts

- **Present:** git 2.54, gh 2.98, node 24.
- **Installed 2026-09-27:** uv 0.12.19 (`~/.local/bin`; pin this version per ADR-0005), Python
  3.13.15 (managed by uv), pnpm 12.5.1 (corepack, via the nvm Node 24.20).
- **Missing:** Docker Desktop (the user installs it), psql (optional; `docker exec` works).
- **Doc tooling:** `scripts/docgen/node_modules` (472 MB, pinned by `package.json`/lock, gitignored)
  and local Google Chrome, which runs offline for diagram rendering. The scripts run on the system
  `python3` (3.9); python-docx is available for checks.
- **Codex:** desktop app data exists in `~/.codex`; there is no `codex` CLI on the PATH, and no
  global `~/.codex/AGENTS.md`. **Open Codex on this folder** (the repo root). Without `.git`, Codex
  reads only the launch folder's AGENTS.md. AGENTS.md is kept under Codex's 32 KiB read limit (18.7 KB
  now).

## Known doc discrepancies

- **Launch date:** Day 45 is **Wed 4 Nov 2026**. The Addendum's "Thu 5 Nov" is wrong.
- **Stale "today" dates:** ARCHITECTURE.md says "Day 2", and CHECKLISTS.md says "Tue 22 Sep".
- **Retro and change windows:** Day 11 is a Thursday, not the "first Friday retro". The MAJOR-change
  windows (Days 14, 28 and 42) fall on Sundays.

## Session log (newest first; at most 5 lines each; keep the newest 5, older ones go to `docs/archive/STATE-log.md`)

- **2026-09-29 Claude (no-git):**
  - Wrote `docs/EXECUTION-PLAN.md` via a 7-agent workflow. gsd-core: don't install it (it contacts
    npm at every session start); 8 of its ideas are ported into our loop.
  - Restructured `docs/` (the user chose a full restructure): 979 references rewritten, and
    `docs/README.md` added. The Word-doc cache moved to `scripts/docgen/.cache/`.
  - AGENTS.md §1: never Artifacts or connectors.
  - GCP: installed `gcloud`/`cloud-sql-proxy`; logged in as `maniacultasset@gmail.com`; created
    projects `aimos-staging` and `aimos-prod`. The client is setting up billing.
  - Files touched (no git): AGENTS.md, README.md, `docs/README.md` (new), `docs/STATE.md`, `docs/plans/README.md`, `.claude/agents/mkos-{planner,implementer}.md`, `.gitignore`, `scripts/{docgen,trace}/*`, every moved doc, `docs/trace/*.map.json`.
- **2026-09-28 Claude (no-git), handoff to Codex:**
  - Did:
    - Spec trace: page level verified; line level has 8,916 statements with verified verdicts →
      REQUIREMENTS-TRACE.md.
    - DATABASE.md rev 3 and BACKEND-ARCHITECTURE.md (both written by workflows).
    - ADRs 0006–0011 as ⚑ proposed; 2 Word docs (with decision highlights); docgen and trace scripts.
    - Codex mapping in the drive skill §5; `.gitignore`.
  - Stopped at the user's request: the build-readiness audit (no output); Codex does step readiness
    inside each plan.
  - Cost: about 19M tokens for the trace (5.9M recording + 12.9M comparison), well over the
    estimates given.
  - Files touched (no git): AGENTS.md, `.gitignore`, `.claude/skills/drive/SKILL.md`,
    `docs/STATE.md`, `docs/adr/0006–0011` + README, `docs/canon/CANON.md`, `docs/review/{TEAM-QUESTIONS,SPEC-TRACEABILITY,REQUIREMENTS-TRACE}.md`,
    `docs/trace/`, `docs/architecture/{DATABASE,BACKEND-ARCHITECTURE}.md`,
    `scripts/{docgen,trace}/`, `deliverables/*.docx`.
- **2026-09-27 Claude (no-git):**
  - Decided ADRs 0001–0005 (GCP; Python 3.13; PG17; Postgres queue/no Redis; hygiene) and the site as
    the source of truth.
  - Built the agent team, `/drive`, the SessionStart hook, `scripts/spec-sync.sh` and the toolchain
    (uv, Python 3.13, pnpm).
  - Mac cleanup (about 7.9 GB).
  - Pending from the user: the iCloud move (on hold).
- **2026-09-26 Claude (no-git):**
  - Read every doc and scaffold file. Verified docs vs scaffold (155 issues confirmed). Evaluated 5
    skill repos (verdict: skip all).
  - Created AGENTS.md, CLAUDE.md (imports AGENTS.md), STATE.md, SUPERSEDED.md, KNOWN-ISSUES.md and
    docs/archive/CLAUDE-v0.md.
  - Rewrote README.md. Applied 45 fixes from a 3-lens review of these files.
