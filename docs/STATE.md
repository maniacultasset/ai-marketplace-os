# STATE — read first, update last

_Last updated: 2026-10-04 (Sun). Any contributor, with any editor, can pick up from here: read "Start here" below. Front door: `docs/PRD.md`, `docs/ARCH.md`, `docs/RULES.md` (the engineering rulebook: it wins over the local agent rules file, and the sources it cites win over it, per its top paragraph), `docs/DESIGN.md`, `docs/PHASES.md`. Docs map: `docs/README.md`. Plan: `docs/EXECUTION-PLAN.md`._
**Active:** none
**Branch:** `main`. S1 follow-ups merge with this PR (squash). S1 itself was PR #3 on 2026-09-30.
**Git repo:** yes. Private `maniacultasset/ai-marketplace-os` (https://github.com/maniacultasset/ai-marketplace-os); first commit `be02277` on 2026-09-29. From here on, every change goes branch → PR → squash merge, and the user approves each merge.
**Hosting ADR:** accepted ADR-0001 (GCP asia-south1, bootstrapped profile; 2026-09-27)
**Tech Lead:** the user (to be confirmed)
**AI lead / FE lead:** not named yet (pending user decision P8)

## Where we are

- **Sprint:** Mon 21 Sep → Wed 4 Nov 2026 (45 days). Day N = today's date − 20 Sep 2026, so Sat
  26 Sep = Day 6.
- **Phase:** **S1 follow-ups merge with this PR.** Next is the pre-S2 docs (P16, P17, P6), then the ADR-0004 spike. The docs
  foundation is complete (2026-09-28).
- **Build location:** this folder is the root of the backend-only repo (ADR-0012).
  - **Never run `scaffold/README.md`'s `cp -R scaffold/.`:** it overwrites the root README. S1 moved
    what it needed; the rest of `scaffold/` is reference only, and later steps take files one by one
    after the `docs/review/SUPERSEDED.md` and `KNOWN-ISSUES.md` fixes.
  - Never copy `scaffold/apps/web` or `scaffold/packages/pipeline`: they belong to the FE and AI repos.
- **Phase 0** (EXECUTION-PLAN §3; derived from the files on 2026-09-30; the user corrects any value):
  - Done: P0-A1 (S1 planned and merged); P0-A2 (spec re-checked 2026-09-30; re-run if older than 3
    days at the S2 plan); P0-U5 and P0-U6 (repo created and pushed as `codekid69`; the bot account is
    deferred, EXECUTION-PLAN decision 2).
  - Partly done: P0-U1 (EXECUTION-PLAN decisions 5, 9 and 10 still open); P0-U2 (OpenTofu not
    installed; libpq not needed, ADR-0013 §7); P0-A3 (done 2026-09-30: the conductor's merge step and
    the local agent rules on by-hand GCP and pushes; open: ask-before-run permissions, the secret-read guard, the
    SUPERSEDED edits);
    P0-A6 (decision 7 and the API-only `JWT_SECRET` rule are T105; the ADR and the rows for decisions
    15 and 16 are open).
  - Open: P0-A5 (the spike), P0-A7 (by D21), P0-G1 (deferred: GitHub stays on Free), P0-G2, P0-G3,
    P0-C1–C5, P0-U3, P0-U4, P0-U7–U10. P0-A4: pending user decision P12.

## Decided (2026-09-26 → 30; ADRs 0001–0005, 0012 and 0013 accepted; 0006–0011 proposed)

- **Agent loop, per step:**
  1. Context pack.
  2. Plan, then a critique of the plan.
  3. **The user approves the plan.**
  4. Tests first, then the implementation.
  5. Gates.
  6. Reviews (correctness/Canon fit, security, test quality), each finding re-checked.
  7. Fix rounds, capped at 3, then `blocked` and the user is asked.
  8. **The user approves the merge.**
  - Reviews run in parallel where the editor supports subagents, and one after another where it
    does not.
- **Editors are interchangeable.** Handoff goes through this file.
- **Third-party agent add-ons evaluated (five), none installed.** The only thing taken is test-first
  discipline (`docs/RULES.md` §8).
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
  - **Spec snapshot:** 2026-09-27 content (`updatedAt` 2026-09-26T11:40:32Z), verified complete (244
    pages); re-checked unchanged 2026-09-30; next check due 2026-10-03.
  - Anything unclear or deviating goes to `docs/review/TEAM-QUESTIONS.md`, which the user takes to
    the team. Our ADRs that depart from the site are provisional until the team confirms them.
- **Conductor (user, 2026-09-27):**
  - The local loop conductor (`drive`) runs the team loop across step ranges (e.g. `S1..S3`) without
    asking between steps.
  - It stops only for batched questions, plan and merge approvals, blockers, or the end of the range.
  - Terminal-only prompts: no push notifications.
  - No token cap; cost is logged per step.
  - A local session-start hook shows this file's status at every session start.
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
  - **No more big doc workflows:** doc gaps close step by step, inside each build step's plan. A
    second editor took over the foundation on 2026-09-28.
- **Front-door docs kept current (user, 2026-09-30):** the user asked that the docs be maintained so
  any contributor, with any editor, can understand the project and take over smoothly. Applied as
  `docs/RULES.md` §12 and the PR template's "Docs" item: a PR that changes behaviour, structure, scope
  or status updates the front-door file and its detailed source in the same PR.
- **GCP by hand (user, 2026-09-29), partly done:** the user wants a cloud DB from day one and a fast
  go-live.
  - Only projects `aimos-staging` and `aimos-prod` exist (created with `gcloud`). Nothing else exists,
    because billing is not active.
  - Once billing is on, the user runs the EXECUTION-PLAN §5.2 decision-5 set under access granted from
    the client's Google account: APIs, budgets, log retention, Cloud SQL PG17 `aimos-db`
    (`db-custom-1-3840`, asia-south1, PITR) with database `aimos` and **no** `app` user (decision 6),
    the `vector` extension check, an Artifact Registry repo `aimos`, and for the fast go-live the
    `aimos-api` account and 2 secrets.
  - This departs from the "reviewed OpenTofu only" rule (`docs/RULES.md` §2). At **S4, `tofu import`
    exactly these resources** (EXECUTION-PLAN §5.5; never create duplicates), then change them only
    through code.
  - **Superseded 2026-09-29:** development and every test use a **local PostgreSQL 17 in Docker** on
    this Mac; Cloud SQL is for **staging and prod** only (user). `cloud-sql-proxy` is for admin access
    to staging.
- **GCP account (2026-09-29):** `maniacultasset@gmail.com`, a personal account with no organisation.
  - Projects **`aimos-staging`** (697366398083) and **`aimos-prod`** (1039991710726) were created by
    the agent with `gcloud`; there is also an unused auto-created "My First Project".
  - **No billing account yet (not active).** The user or client creates one in the console, then we
    link it.
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
  - **The agent ran `git init`, the first commit, `gh repo create` (private) and the first push**, a
    one-time exception to the agent rules (AGENTS.md §7) authorised by the user.
  - Repo settings: squash-only, delete branch on merge, wiki/projects off, dependency vulnerability
    alerts on, Dependabot auto-fix PRs off (Renovate per decision 17).
  - **Two build tracks: yes.** The ADR-0011 + agent-rules (AGENTS.md §7) amendment is to be drafted
    for the user's approval before S2 (pending user decision P6).
  - Recommended defaults applied: trunk-based branching (decision 3); no `gcloud`-created `app`
    database user (decision 6); generated `.docx` files not in git (decision 19, `.gitignore`).
- **v1 expectations confirmed (user, 2026-09-29):** v1 = the site's Day-45 Definition of Done (L4679).
  - The journey: sign in → connect Amazon/Flipkart → import → AI generate → unified errors → bounded
    self-correction → maker-checker approval → publish with per-field results and operational
    analytics, in production.
  - Scope follows the Phase-1 scope lock (L4723).
  - We build backend + DB and the hand-off points; the AI and FE teams build their parts, and we
    monitor them.
  - The launch-scope choice (Amazon-first on 4 Nov vs full scope around 18–25 Nov) is the client's, by
    Sun 4 Oct.
- **S1 plan decisions (user, via the loop conductor, 2026-09-29):**
  - Approved with the Tech Lead's fixes: all 18 logged critique findings are applied before the final
    approval.
  - **ADR-0013 accepted:** settings by role, no FE keys, staged checks on Free, the ENV.md edits,
    T105/T106, the dependency set, and the pip-audit/Renovate package-name egress exception.
  - The three-lead rule is **waived for the empty `packages/schemas` 0.0.0 skeleton only**.
  - **Step plan files `docs/plans/<step>/` are local-only** (`.git/info/exclude`); PR descriptions
    carry the scope and results.
- **Editor quality add-ons (user, 2026-09-29):** a Python language-server add-on (plus `pyright`
  1.1.414 via npm), a PR-review add-on and a security-review add-on, in the local editor config only.
  - Skipped: five others, including one that posts AI-signed PR comments.
  - CI tooling in the repo stays the source of truth for pass/fail, for every editor.
- **Build order (user, 2026-09-29): local-first, plus an early thin deploy.**
  - **Phase A:** build and prove every API, schema and migration locally (Docker Postgres 17), end to
    end with the AI stub.
  - **Once billing is on:** deploy only the skeleton (health check + DB connection) to staging, so
    hosting problems surface early.
  - **Phase B:** full staging and prod hosting after the local end-to-end run. The frontend moves from
    the S3 mock to the live URL.
  - **Phase C:** swap the AI stub for the AI team's pinned pipeline.
  - Production-grade throughout: versioned API/contracts, additive-only migrations, ADRs, and
    tests + approval on every merge.
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
  - The local agent tooling (`AGENTS.md`, the editor config folders and entry files, `docs/plans/*/`)
    stays out of git through `.git/info/exclude`.
  - No AI co-author lines in commits or PRs; tracked files use neutral terms ("the agent", "the
    editor", "contributors").
  - README and description rewritten.
  - The first two commits (`be02277`, `ef0f3be`) still contain the tooling files in history. Rewriting
    them needs a force-push, which the editor's safety guard blocked; the user decides (open decision
    11).
- **Hosting research done:** `docs/research/hosting-2026-09.md`.
  - Recommended: **AWS Mumbai + Activate credits**, plus Microsoft credits for Azure OpenAI tokens.
  - **Conditional on** the client being incorporated and pursuing DPIIT, and on team AWS skills.
  - **Decided: ADR-0001, GCP asia-south1.** The user delegated the choice: incorporation is
    unknown, the user knows GCP a little, and year-2 cost is equal to AWS. Estimate about
    $195–205/mo (prod + staging) before GST. Credits: $300 trial (90 days) + $2k Start tier
    (discretionary). Azure OpenAI credits are to be proposed to the AI team.

## Open decisions

Cite these as "STATE open decision N". `docs/EXECUTION-PLAN.md` §2 has its own numbered list, cited
as "EP decision N" (EP decision 10, the S2 contract freeze, is a different item from row 10 here).

| # | Decision | Owner | Blocks |
|---|---|---|---|
| 1 | ~~Hosting~~ **Decided:** ADR-0001, GCP asia-south1. | — | — |
| 2 | ~~App stack~~ **Decided:** ADR-0002 to ADR-0005 (Python 3.13, PG17, procrastinate/no Redis, hygiene). | — | — |
| 3 | ~~Docker runtime~~ **Done 2026-09-29:** Docker Desktop 4.93.0 installed (free under 250 staff and $10M revenue). | — | — |
| 4 | GitHub Team before the first prod release; a bot account before any prod credential (EP decisions 1–2). ~~`git init`, the private repo, the branch model~~ done 2026-09-29 (trunk-based). | user / client | S25, the first prod credential |
| 5 | Status of client items is **unknown**: incorporation/GST/DPIIT, apex domain, Amazon and Flipkart applications (due 23 Sep), cloud billing account (**not active**; blocks S4 apply, the thin staging deploy, S12 and S25) and credit applications (ADR-0001 plan), OpenAI key, launch categories, UAT users. By its own wording, Addendum interim rule (3), the fallback apex, has triggered. **Choosing an apex is the user's call; agents never pick or register one.** | user → client | S4, S10, S12, adapters |
| 6 | Vision: v1 is human approval with `auto_publish` reserved (decided 2026-09-27). The **client** still has to confirm, since the Founder BRD says "autonomous submission" and uses a 14-status model. | client | long-term roadmap |
| 7 | Was the kickoff pack sent to the client? Its B1 blocks carry the wrong KMS ring and `/v1/marketplace` redirect URIs. Were they used to file with Amazon or Flipkart? | user | marketplace filing |
| 8 | ~~Multi-brand logins~~ **Decided 2026-09-27:** memberships, designed now. | — | — |
| 9 | **AI team:** confirm Python + LangGraph in-process, **Python 3.13** (ADR-0002), and **Azure OpenAI** for Microsoft credits (the user will propose it). | user → AI team | S2 (the seam), S13 |
| 10 | **Schema source for S5–S10:** accept `docs/architecture/DATABASE.md` rev 3 as the schema source, building its `[Proposed]` items as written until reviewed. `mkos-planner` uses it only once STATE marks it accepted. Until then it falls back to the CANON.md migration ledger, which predates the 2026-09-27 decisions. | user (Tech Lead) | S5 onward |
| 11 | Rewrite `be02277`/`ef0f3be` to drop the tooling files from history? It needs a force-push to `main`; agents never do it. | user | none |

## Decisions of 2026-09-30 (were pending from the handoff check; now decided)

**Decided by the user on 2026-09-30: every "Recommended default" below is accepted as written, except
P8.** P8 is the interim rule stated in its row. The "Changes once decided" column lists the files that
change; where an item is work, it appears in "Start here" item 2.

Done on 2026-09-30:
- **P1:** the pending doc edits rode in PR #4.
- **P11:** tool names and local paths removed in PR #4.
- **P12:** `gitleaks dir --redact .` found 2 matches, both false positives: a spec sentence about the
  Listing record's parent-child field in `docs/review/REQUIREMENTS-TRACE.md:520` and
  `docs/trace/c02.json:2253`. They are already allowlisted for commit `be02277` in `.gitleaksignore`.
  There are no real secrets. Allowlisting them joins the S1 follow-ups.
- **P14:** backup at `~/Backups/ai-marketplace-os/local-only-2026-09-30-2325.tar.gz` (owner-only).
- **P18:** the local rule files are fixed, and the session-start status prints the rules version.

| # | Decision | Recommended default | Changes once decided |
|---|---|---|---|
| P1 | How STATE and EXECUTION-PLAN edits reach `main`, and which PR carries the uncommitted 2026-09-30 doc edits | They ride in the current work branch's PR; with no branch open, a `docs/state-<YYYY-MM-DD>` branch and a docs-only PR ("docs-only, no gates run"). Never commit them to `main`; the committed `Active` line is always `none`. The pending edits go on `docs/state-2026-09-30`. **Blocker for any handover:** until this PR merges, a clone of `origin/main` has no front door (README "Start here"; PRD, ARCH, RULES, DESIGN and PHASES are untracked), so anyone taking over works only in this folder. | `docs/RULES.md` §10; the local agent rules (§6, §7) and the conductor's "Record" step |
| P2 | MinIO root credentials | Non-default values (the round-3 security review): compose sets `MINIO_ROOT_USER: ${S3_ACCESS_KEY}` and `MINIO_ROOT_PASSWORD: ${S3_SECRET_KEY}`, interpolated from `.env` with no new Settings keys (`extra="forbid"` rejects them). The alternative is `minioadmin`/`minioadmin` as in `docs/canon/ENV.md:51-52`. Changing that Canon-tier row needs the user's OK. | `docker-compose.yml`, `.env.example:42-43`, `docs/canon/ENV.md:51-52`, `apps/api/app/core/config.py:80-81`, T107 |
| P3 | MinIO healthcheck and one-shot init form (the images have no shell and no `mc ready`) | `minio`: a liveness healthcheck running the server binary's `--version` (check its path in the image on build day). `minio-init`: `restart: on-failure:10`, running `mb --ignore-existing` until the server answers. `bootstrap.sh` asserts `minio-init` exited 0, and `test_compose.py:66-69` is amended to allow `restart: on-failure` one-shots. | `docker-compose.yml`, `scripts/ci/bootstrap.sh`, `apps/api/tests/meta/test_compose.py`, T107 |
| P4 | OK for S1 follow-ups (a)–(c) as an extension of the S1 approvals (A10), and how they run | A new plan id `s1-followups`: a short PLAN.md and CODE-PLAN.md (line-1 status line; an approvals row for the Chainguard images and the `editables` pin), a fresh budget of 3 fix rounds, reviews as usual. The alternative: the conductor's "with guidance" path on id `s1` (one implementer fix, gates, code and security review of the touched files, a verifier). | `docs/plans/s1-followups/` (local-only), "Start here" 2.1 |
| P5 | Step order: may the S2 plan start before the ADR-0004 spike? | Single-track until P6 is settled. Order: 2.1, then the S2 plan (plans are docs-only and may overlap), then the spike, then the S2 build. S4 waits for billing (P10). | "Start here" 2, the `docs/PHASES.md` step table, the local conductor's scope rule |
| P6 | Two build tracks: draft the amendment, or stay single-track | Before the S2 plan is approved, draft as `Status: proposed` the ADR-0011 amendment and the agent-rules text (EP decision 8): one conductor; an `Active` suffix per track; a git worktree per track with its own compose project and DB port (a Canon edit to `CANON.md:65`, which needs the user's OK); only Track A writes migrations; only the conductor edits STATE. Trip-wire: D12, Fri 2 Oct (EXECUTION-PLAN §6.5). | `docs/adr/0011-team-loop.md`, `docs/canon/CANON.md`, the local agent rules §7, the `docs/PHASES.md` step table |
| P7 | ADRs 0006–0008: build as written now, or wait for the team verdict; and the S2 freeze scope (EP decision 10) | Build as written (their status lines say so); a later team overrule becomes an additive migration. S2 freezes the 0006–0008 shapes as written. | `docs/canon/CANON.md:27`, "Start here" 2.5, EXECUTION-PLAN §6.2 (S2, S6 rows) and §10; if "wait": the ADR-0006/0007/0008 status lines |
| P8 | Who the AI lead and FE lead are, and how their contract approval is recorded (GitHub teams and CODEOWNERS are inert on Free) | **Decided (user, 2026-09-30):** our team owns the backend and database; the frontend and AI teams work independently in their own repositories and have no named leads yet. **Until they are named, the Tech Lead (the user) approves `packages/schemas` changes.** Every contract release ships a changelog entry and a notice to both teams; they adopt versions when ready (pinned); MAJOR changes still only in the Day-14/28/42 windows. | this file's header, `docs/RULES.md` §11, the local agent rules §6 |
| P9 | Do S5 and the spike complete on local Docker PostgreSQL, with their staging legs deferred to billing? Is T3 decided on the spike's local half? | Yes to both; the staging legs (S5 `0000` + upgrade on `aimos-db`; spike criterion 6) are tracked in "Start here" 2.9. | EXECUTION-PLAN §6.2 (S5 row) and §10, "Start here" 2.2 and 2.9, T3 |
| P10 | S4: where it sits in the order, and who runs `tofu plan` | S4 code after S3 (Track B), checked locally with `tofu fmt -check` and `tofu validate` once OpenTofu is installed. `tofu plan`/`apply` are run by the user after billing, since agents hold no GCP credentials (EXECUTION-PLAN §5.1, §8). | "Start here" 2.9, EXECUTION-PLAN §6.2 and §10 |
| P11 | Remove the AI tool names left in tracked files | One docs PR: `docs/architecture/DATABASE.md:3`, the two temp paths in `docs/drafts/CHECKLISTS.md:131` and `:162`, and the retired v0 rules file in `docs/archive/`, whose name carries a tool name (rename it and update its references). The client spec snapshot (`docs/source/`, `docs/trace/`) is never edited. (`BACKEND-ARCHITECTURE.md` §12's ADR-0011 row was made neutral on 2026-09-30, since that file was edited.) | those files |
| P12 | P0-A4: was the read-only gitleaks tree scan run before the first commit? | Treat it as not recorded: run `gitleaks dir --redact .` (read-only) and report the result. | "Where we are" (Phase 0) |
| P13 | A mechanical guard against AI attribution in commit messages | A commit-msg check that rejects AI co-author trailers and "Generated with" lines, added to the existing pre-commit `commit-msg` hook through the normal loop. | `.pre-commit-config.yaml`, a script under `scripts/ci/` |
| P14 | A backup of the local-only files (rules file, editor config, `docs/plans/`) | **Now:** a dated archive kept on this Mac outside the repo, made by the user (or by an agent on the user's go). The loop, the roles and every step record exist only in these files (see P16). | none in the repo |
| P15 | Conflict-order rank of the docs `docs/README.md` doesn't rank: `docs/architecture/`, the body of `docs/EXECUTION-PLAN.md`, proposed ADRs 0006–0011 | After rank 7 (`canon/ENV.md`) and before `drafts/`, in this order: proposed ADRs 0006–0011 as written (P7); `docs/architecture/` (`DATABASE.md` is the schema source only after open decision 10); the EXECUTION-PLAN body (its §2 items bind once recorded here). A newer STATE decision or accepted ADR always wins over an older detailed doc. | `docs/README.md` "When sources disagree", `docs/RULES.md` top, the local agent rules §2 |
| P16 | A tracked, tool-neutral process doc | Commit `docs/PROCESS.md` (roles, the loop, the session protocol, approvals; no editor names) and repoint the tracked `AGENTS.md §N` citations (112 lines in 23 files outside `docs/trace/` and `docs/source/`, including the lint messages at `pyproject.toml:60-62`) to it or to `docs/RULES.md`; keep only editor-specific glue local. Today a clone can't run the committed loop (ADR-0011, `docs/plans/README.md`), and committed tests cite local-only files ("Start here" 2.1a). This revisits the 2026-09-29 local-only decision, so it needs the user; make the P14 backup first. | a new `docs/PROCESS.md`, the 23 files, ADR-0011, `docs/plans/README.md`, the local agent rules |
| P17 | One tracked list of acceptance criteria before the S2 plan | Bring P0-A7 forward: write the corrected "Done when" lines for S2–S14 (BUILD-SEQUENCE after SUPERSEDED, KNOWN-ISSUES and the ADRs; the draft still names Python 3.11, pg16, Redis/arq, `apps/web`, `packages/pipeline` and "eight contracts") and draft S15–S26 (with S14a/b, S17a/b and S25) in one tracked file, an amended BUILD-SEQUENCE or a new `docs/STEPS.md`; the user approves. | `docs/drafts/BUILD-SEQUENCE.md` or the new file, `docs/PHASES.md`, `docs/RULES.md` §2, EXECUTION-PLAN P0-A7 |
| P18 | Local-only file fixes (they take effect only in a new session) | The local agent rules §0: replace "the authority for engineering rules" with the `docs/README.md` wording. The local editor entry file's examples (`S2..S3`, a plan for S2) point to "Start here" 2 instead of naming steps. The session-start hook also prints the rules file's "Rules version" line, so a stale session shows; restart any session whose loaded rules lack `Rules version: 2026-09-30`. | the local agent rules, the editor entry file, the session-start hook |
| P19 | `uv run --frozen` in the Makefile, and a `make hooks` target | Add both to the S1 follow-ups under P4's process. The make targets call `uv run` without `--frozen` while the hooks use it (`.pre-commit-config.yaml:11-21`), so a local run may re-lock; today only `make dev` installs the hooks. README "Quickstart" documents the manual commands meanwhile. | `Makefile`, README "Quickstart", "Start here" 2.1 |

## Next actions

**Start here (any contributor, any editor, from 2026-09-30):**
0. **Session start, every time:**
   - Read the front door in this order: `docs/PRD.md`, `docs/ARCH.md`, `docs/RULES.md` (the
     engineering rulebook), `docs/DESIGN.md`, `docs/PHASES.md`, then this file. `docs/README.md` maps
     every other doc. Agents on this Mac also read the local `AGENTS.md`.
   - Run `git fetch --prune` and `git status`. If `main` shows uncommitted STATE or EXECUTION-PLAN
     edits, don't commit them to `main`; ask where they go (P1).
   - Before your first edit set `**Active:** <editor> · <branch> · since <YYYY-MM-DD HH:MM> · <task>`;
     at the end set `**Active:** none`.
   - Start a fresh editor session after any edit to the local agent rules; they load once.
   - The spec snapshot was re-checked unchanged on 2026-09-30; run `scripts/spec-sync.sh` if it is more
     than 3 days old (next due 2026-10-03).
   - **Open this exact folder.** A fresh clone lacks the local-only files listed in this Mac's
     `.git/info/exclude` (`AGENTS.md`, the editor config folders and entry files, `docs/plans/*/`).
     Copy them from this Mac; never recreate them from memory (backup: P14). Until P1 lands, a clone
     also lacks the front door itself.
   - Answer or chase the "Pending user decisions" above before the work they block.
1. **Where we are:**
   - **S1 is merged into `main` (PR #3, 2026-09-30):** 10/10 CI checks green on GitHub and 325 tests.
   - The design is complete (`docs/architecture/`, ADRs 0001–0013, `docs/EXECUTION-PLAN.md`); the
     implementation is about 5% done.
   - Local-only on this Mac:
     - S1 plan, report and deviations: `docs/plans/s1/{PLAN,CODE-PLAN,REPORT,DEVIATIONS}.md`;
     - the role files (`mkos-*.md`) and the loop conductor, in the local editor config (the
       conductor's §5 = how to run the loop by hand in an editor without subagents).
2. **v1 build order: local-first** (user, 2026-09-29/30). Build and prove everything on this Mac with
   Docker, using stand-ins for anything external. Order and dependencies come from
   `docs/EXECUTION-PLAN.md` §6.2–§6.3, and step scope and "Done when" from
   `docs/drafts/BUILD-SEQUENCE.md` (S1–S14) as corrected by `docs/review/SUPERSEDED.md` and
   `KNOWN-ISSUES.md`.
   1. **S1 follow-ups — done in this PR** (plan id `s1-followups`, branch `fix/s1-followups`; P2–P4, P13 and P19 included; `parent/child` allowlisted in `.gitleaks.toml`). Record of what landed:
      - (a) **MinIO:**
        - Use `cgr.dev/chainguard/minio` + `cgr.dev/chainguard/minio-client`, pinned by index digest
          and bound to 127.0.0.1, with services `minio` and `minio-init`.
        - The images have no shell: init with `MC_HOST_local` + `mc mb --ignore-existing`; the
          healthcheck can't use `mc ready` (form: P3). Root credentials: P2.
        - Volume: on build day, check whether `/data` exists in the image and is owned by uid 65532
          (`docker image inspect` or an image export). If not, mount `miniodata` at an existing
          65532-owned path and pass that path to `server`; record the departure from site L7022 in
          T107 and ask.
        - Bucket check (proposed; verify on build day), for each of `marketplace-images` and
          `marketplace-csv`: `docker compose run --rm --no-deps -e
          MC_HOST_local=http://$S3_ACCESS_KEY:$S3_SECRET_KEY@minio:9000 minio-init ls local/<bucket>`.
          Read `minio-init`'s exit code from `docker compose ps -a --format json minio-init`. The
          approved `--entrypoint sh` form can't run in a shell-less image.
        - Also: the planned tests `test_services_exactly_three`,
          `test_minio_pinned_by_digest_with_site_command_ports_volume`,
          `test_init_creates_both_buckets_idempotently` and
          `test_scripts.py::test_bootstrap_checks_minio_init_and_both_buckets`; the
          `scripts/ci/prove_gates.sh` bootstrap leg for a misspelled `marketplace-csv`; the
          R07014/R07015/R07021/R07022/R07027 verdicts in `docs/trace/c11.map.json`, then
          `check_mapping.py` and `compile_trace.py`; the MinIO line in README.md "Quickstart".
        - Add T107 (image source) to TEAM-QUESTIONS. Its Impact column: the free tier serves only the
          latest tag, so if a pinned digest disappears, re-pin through a Renovate run or by hand in a
          `fix/` PR; `bootstrap` failing with "manifest unknown" is the signal.
        - Replace the local-only citations in committed tests (`test_compose.py:3-4` and
          `test_scripts.py:3` cite DEVIATIONS.md; `test_readiness_cancellation.py:3` cites CODE-PLAN.md
          T5) with T107 or the acceptance-criterion text.
        - This closes AC9 (bucket half), AC10 and the AC15 bucket leg.
      - (b) **Pin `editables`:** `editables==<exact 0.x, resolved on build day>` in the root
        `pyproject.toml` `build-constraint-dependencies` (a specifier only; `uv.lock` stores no
        hashes), and hashed in `build-constraints.txt` (add it to the recipe input at :5; "four
        dependencies" becomes "five" at :2-3). Add `editables` to `BUILD_CONSTRAINT_NAMES` in
        `apps/api/tests/meta/test_workspace.py`, then `uv lock`.
      - (c) **pip-audit:** add `-s osv --timeout 60` in `scripts/ci/run_pip_audit.py`. Test first: a
        unit test that monkeypatches `subprocess.run` and asserts `-s osv --timeout 60` in the
        `pip_audit` argv.
   1b. **Before the S2 plan, in this order** (decided 2026-09-30):
      - **P16:** commit a tool-neutral `docs/PROCESS.md` (roles, loop, session protocol, approvals) and
        repoint the tracked `AGENTS.md §N` citations to it or to `docs/RULES.md`.
      - **P17:** one tracked list of corrected "Done when" lines for S2–S14, plus drafts for S15–S26,
        which the user approves.
      - **P6:** draft the two-track amendment as `Status: proposed`. Single-track until it is approved
        (P5).
   2. **ADR-0004 spike** (1 day; the local half, EXECUTION-PLAN P0-A5): procrastinate transactional
      enqueue against the 8 pass criteria, in a throwaway harness outside the repo; report in
      `docs/plans/spike-0004/REPORT.md` (local-only). It decides the queue (T3) before S8/S13. The
      staging half (criterion 6) waits for billing (2.9). Open: T3 on the local half (P9); may the S2
      plan come first (P5).
   3. **S2**: freeze `mkos-schemas` v1.0.0. Needs EP decision 10 (freeze all 17 modules as written, or
      only the decided ones; P7), T1 and T61 (EXECUTION-PLAN §6.2); target the Sun 4 Oct (D14) MAJOR
      window. Before the S2 plan: the two-track choice (P6) and the AI and FE leads (P8). From here,
      `packages/schemas` changes need Tech Lead + AI lead + FE lead.
   4. **S3**: freeze the `/api/v1` OpenAPI, publish `@mkos/schemas` to GitHub Packages (ADR-0012), and
      stand up the mock the FE team integrates against.
   5. **S5 → S6 → S7 → S8**: Alembic, roles and migrations 0001–0003b.
      - Needs **STATE open decision 10:** accept `DATABASE.md` rev 3 as the schema source.
      - Needs the ADR-0006/0007 question settled: build as written, or wait for the team (P7).
   6. **S9, S10**: repository guard, isolation gate, seed; auth, RBAC, maker-checker, 0004/0005 (RLS).
   7. **S11, S13, S14a/b, S17a**: observability, job spine + CSV import, rules engine, AI seam with the
      **stub pipeline**, Pillow checks.
   8. **S15–S24 and S26** (EXECUTION-PLAN §6.3). **These steps have no "Done when" lines yet: the planner writes
      them first (P0-A7).**
      - Build against stand-ins: a **fake marketplace server and FakeAdapter** instead of the SP-API
        sandbox or Flipkart, a **local key** instead of Cloud KMS, **MinIO** instead of GCS, and the
        **stub pipeline** instead of the AI team's drop.
   9. **Waits on the client or other teams; don't start without it:**
      - **GCP billing** → the thin staging deploy of the skeleton (EXECUTION-PLAN §5.2 step 9, run by
        the user), S4 apply, S12 (which also needs S10, S11 and DNS), S25 prod, and the staging legs
        of S5 (`0000` + upgrade on `aimos-db`) and the spike (criterion 6). S4 OpenTofu code may be
        written and checked locally (`tofu fmt -check`, `tofu validate`) once OpenTofu is installed;
        `tofu plan`/`apply` need GCP credentials, which agents never hold (EXECUTION-PLAN §5.1, §8).
        Where S4 sits in the order: P10.
      - **Domain** → HTTPS and the OAuth redirect URLs.
      - **Amazon / Flipkart developer credentials** → real OAuth (S15) and sandbox publish (S20/S22).
      - **AI team's pipeline** → swap the stub for the pinned version (T104).
3. **How to run each step (never skip):**
   - Plan (CONTEXT, PLAN, CODE-PLAN in `docs/plans/<id>/`, local-only).
   - Critique, then **the user approves** (line 1 of CODE-PLAN.md).
   - Tests first, then the code.
   - All 10 gates, reviews, and at most 3 fix rounds, then REPORT.md.
   - Branch → PR (plain engineering text) → CI green → **the user approves the merge** (squash).
   - No AI names or attribution in commits, PRs or committed files.
   - Rules: `docs/RULES.md` (engineering) and `docs/plans/README.md` (the loop).
4. **The user takes to the team and client:**
   - **This week:** name the AI and FE leads (P8) before the S2 plan, and take the S2/S5 rows (T1, T3,
     T7, T23, T58, T59, T61; EXECUTION-PLAN §6.6 "Team questions by step") to the team. All 106
     TEAM-QUESTIONS rows are still open, so every point runs on its default.
   - TEAM-QUESTIONS T1–T106 (T107 when added) and the ⚑ proposed ADRs 0006–0011.
   - **Launch scope** (Amazon-first 4 Nov vs full scope around 18–25 Nov) **by Sun 4 Oct**.
   - Open decisions 4, 5, 7, 9, 10 and 11 above, EP decision 10, and the "Pending user decisions".
   - **File the Amazon SP-API and Flipkart developer applications now**: Amazon's review takes 2–4
     weeks.
5. **Doc gaps close step by step:** each step's plan pulls in its rows from
   `docs/review/REQUIREMENTS-TRACE.md`. When a verdict changes, re-run `scripts/trace/check_mapping.py`
   and `compile_trace.py`.

**Done (2026-09-26 → 28):**
- **Research:**
  - Context brief (`KNOWN-ISSUES.md`, `SUPERSEDED.md`).
  - Competitor analysis (`docs/research/catalogus.md`).
  - Stack and hosting research (`docs/research/`).
- **ADRs:**
  - 0001–0005 accepted and recorded in CANON.md "Amendments by ADR".
  - 0006–0011 proposed.
- **Tooling:** the local agent tooling (role files, the loop conductor, the session-start hook) and
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
- **Installed 2026-09-29:** Docker Desktop 4.93.0 (engine 29.8.1, Compose 5.5.1; the user accepted the license; turning off usage statistics in Settings is up to the user), gitleaks 8.30.1 (`~/.local/bin`, checksum-verified), pyright 1.1.414, `gcloud` 587 (usage reporting off) and `cloud-sql-proxy` 2.26.0 (`~/.local/bin`). Smoke test: PostgreSQL 17.10 + pgvector 0.8.5 OK.
- **Not installed:** OpenTofu (needed for S4; P0-U2).
- **Not needed:** host `psql`/libpq; ADR-0013 §7 runs it inside the compose container.
- **Doc tooling:** `scripts/docgen/node_modules` (472 MB, pinned by `package.json`/lock, gitignored)
  and local Google Chrome, which runs offline for diagram rendering. The scripts run on the system
  `python3` (3.9); python-docx is available for checks.
- **Second editor:** its desktop app data is in the home directory; it has no CLI on the PATH and no
  global rules file. **Open it on this folder** (the git root): it reads the root `AGENTS.md`
  (local-only, on disk). `AGENTS.md` is kept under 32 KiB, that editor's default read limit
  (17 KB now).

## Known doc discrepancies

- **Launch date:** Day 45 is **Wed 4 Nov 2026**. The Addendum's "Thu 5 Nov" is wrong.
- **Stale "today" dates:** ARCHITECTURE.md says "Day 2", and CHECKLISTS.md says "Tue 22 Sep".
- **Retro and change windows:** Day 11 is a Thursday, not the "first Friday retro". The MAJOR-change
  windows (Days 14, 28 and 42) fall on Sundays.

## Session log (newest first; at most 5 lines each; keep the newest 5, older ones go to `docs/archive/STATE-log.md`)

- **2026-10-04 (s1-followups):** MinIO in compose (Chainguard, `/data` despite uid 0), `editables==0.6`, pip-audit on OSV, commit-msg attribution check, frozen `uv run`. The user approved the plan and the merge. README wait is scoped to Postgres. Next: P16, P17, P6, then the ADR-0004 spike.
- **2026-09-30 (docs standard):** PR #4 adds PRD, ARCH, RULES, DESIGN and PHASES and the handoff fixes. The user accepted all P1–P19 defaults, with P8 as an interim rule (the Tech Lead approves contracts until the leads are named). P12 scan clean (2 false positives), P14 backup made, P18 local fixes applied.
- **2026-09-30 (verified-check fixes; tool-neutral docs):**
  - Applied the verified-check findings: front door (PRD, ARCH, RULES, DESIGN, PHASES), both READMEs, EXECUTION-PLAN (§1 and the 10-check count fixed in place; the add-on no longer named), BACKEND-ARCHITECTURE (10 checks), ADR-0010/0011 amendment notes, the PR template. Items needing the user: P15–P19; P1, P8 and P14 sharpened.
  - Files touched: README.md, `docs/{STATE,README,EXECUTION-PLAN,PRD,ARCH,RULES,DESIGN,PHASES}.md`, `docs/architecture/BACKEND-ARCHITECTURE.md`, `docs/adr/{0010,0011}-*.md`, `docs/archive/STATE-log.md`, `.github/pull_request_template.md`. Local-only files unchanged (P18).
  - Docs-only, no gates run. Not committed (P1). The Word editions are not rebuilt.
- **2026-09-30 (handoff check applied; tool-neutral docs):**
  - Applied the doc fixes from the 2026-09-30 handoff check; 14 items that need the user are listed under "Pending user decisions".
  - README.md and `docs/README.md` now open with the front door (PRD, ARCH, RULES, DESIGN, PHASES, STATE); the PR template has a "Docs" item; the local agent rules point to `docs/RULES.md`; the conductor's merge step is the PR flow.
  - Files touched: README.md, `docs/{STATE,README,EXECUTION-PLAN,PRD,ARCH,RULES,DESIGN,PHASES}.md`, `docs/plans/README.md`, `docs/adr/0011-team-loop.md`, `docs/archive/STATE-log.md` (new), `.github/pull_request_template.md`; local-only: AGENTS.md, the editor config (conductor, role files, hook, plan workflow), `docs/plans/s1/{REPORT,LOOP}.md`.
  - Docs-only, no gates run. Not committed (P1).
- **2026-09-29/30 (no AI attribution in git):**
  - Setup:
    - GitHub repo made backend-only, with no AI branding (PRs #1, #2 merged).
    - Docs restructured.
    - GCP projects created; billing still pending.
    - Docker, gitleaks and pyright installed.
  - **S1:** planned (approved with 18 fixes, ADR-0013 accepted), then built by the loop: 3 fix rounds;
    the report was written by hand after the report step stalled 3 times.
  - **Merged as PR #3.** Three follow-ups are listed under "Start here".
  - Files touched: the S1 tree (97 files + fix round), `docs/STATE.md`, ADR-0012/0013, TEAM-QUESTIONS,
    CANON note, README files; local-only: AGENTS.md, the editor config, `docs/plans/s1/`.
- **2026-09-29 (no-git):**
  - Wrote `docs/EXECUTION-PLAN.md` via a 7-agent workflow. A third-party agent-workflow add-on: don't
    install it (it contacts npm at every session start); 8 of its ideas are ported into our loop.
  - Restructured `docs/` (the user chose a full restructure): 979 references rewritten, and
    `docs/README.md` added. The Word-doc cache moved to `scripts/docgen/.cache/`.
  - AGENTS.md §1: never publish to hosted document or connector services.
  - GCP: installed `gcloud`/`cloud-sql-proxy`; logged in as `maniacultasset@gmail.com`; created
    projects `aimos-staging` and `aimos-prod`. The client is setting up billing.
  - Files touched (no git): AGENTS.md, README.md, `docs/README.md` (new), `docs/STATE.md`, `docs/plans/README.md`, the local role files (planner, implementer), `.gitignore`, `scripts/{docgen,trace}/*`, every moved doc, `docs/trace/*.map.json`.