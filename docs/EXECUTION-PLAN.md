# EXECUTION-PLAN: from setup to launch (backend + DB)

_Written 2026-09-29 (Day 9) by the agent from four drafts (a third-party agent-workflow add-on,
GitHub/CI/CD, GCP, roadmap) and two critiques (feasibility, security/DevOps). Facts re-checked against
the repo on 2026-09-29; §1 and the §6.2 S1 row updated in place on 2026-09-30 (§10)._

**Status: proposed**, except the §2 decisions the user has made (1–4, 6–8, 17 and 19; STATE "Setup
decisions" and §10). Nothing here overrides AGENTS.md, an accepted ADR or the Canon until the user
approves the matching item in §2. **This file is the plan. `docs/STATE.md` is the status.** Progress
(which `P0-*` item or step is done) is recorded in STATE, never ticked in this file. The plan changes
only with the user's OK, and each change gets a dated line in §10.

Legend: **[GO]** = acts outside this machine (the client's GitHub or GCP), so it needs the user's
explicit go for that exact command. **[CLIENT]** = only the client can do it.

---

## 1. In one page

1. **S1 merged on 2026-09-30 (PR #3):** repo, toolchain and the 10 CI checks (ADR-0012). The Phase 0
   items still open are in STATE "Where we are"; the next work, in order, is STATE "Start here" 2.
2. **You and the client, open:** Google billing (not active; it blocks S4 apply, the thin staging
   deploy, S12 and S25) and access for **your own** Google identity; then you run the GCP runbook
   (§5.2). GitHub stays on Free until the first prod release, and agents push as `codekid69` until the
   **bot account** exists (decisions 1–2 as decided). Docker, gcloud and gitleaks are installed and
   the repo exists.
3. **GitHub:** one private repo, `maniacultasset/ai-marketplace-os`, trunk-based. Every change is a PR
   that must pass the 10 checks (advisory on Free, ADR-0013 §5). You approve and merge. From S4, a
   merge to `main` auto-deploys staging. Prod deploys only the release tag `vX.Y.Z` that you publish,
   with smoke test, watch and auto-rollback.
4. **Safety:** agents never hold your GitHub login or your Google admin login. Prod credentials go only
   to your release. No key files anywhere. Secrets live only in Secret Manager.
5. **Staging:** every date waits on GCP billing (§10). Once billing is on, the by-hand thin deploy
   (§5.2 step 9) comes first. The planned targets were auto-deploy from `main` about **Thu 8 Oct
   (D18)** and the walking skeleton over TLS about **Thu 15 Oct (D25)**, if the domain exists by Fri
   9 Oct.
6. **Prod environment**, the first practice release and a rollback drill: planned about **Thu 15 Oct
   (D25)**; it also waits on billing and the domain.
7. **First AI-drafted listing on staging:** about **Thu 22 Oct (D32)**, if the AI team's pipeline
   arrives by Mon 19 Oct.
8. **First publish to the Amazon sandbox:** about **Tue 27–Wed 28 Oct (D37–38)**. A real Amazon
   publish also needs Amazon to approve the app.
9. **Does 4 Nov hold?** Only as an **Amazon-first launch** with the cuts in §6.6, **two build tracks**
   (decision 8) and same-day approvals. That leaves 1–2 days of slack. The full scope realistically
   lands **mid to late November**. Decide on **Sun 4 Oct (D14)**, not later.
10. **The third-party agent-workflow add-on: don't install it.** It contacts npm at every session
    start and conflicts with our approval and git rules. We copy 8 of its ideas into our own loop instead (§7).
11. Every editor and agent follows this file. Status lives in `docs/STATE.md` (§8).

---

## 2. Decisions for the user

Each has a recommended default. "By" is the latest date that keeps §6 on track. Other files cite these
as "EP decision N"; `docs/STATE.md` has its own "open decision" list.

### Needed this week

| # | Decision | Recommended default | By |
|---|---|---|---|
| 1 | GitHub plan | **Team**: ~7 seats × $4 ≈ $28/mo, plus ≈ $36/mo of Actions minutes. On Free, private repos get no rulesets, required checks, CODEOWNERS or environments, so S1's "force-push to main is rejected" (BUILD-SEQUENCE.md:20) can't pass. If the client stays on Free: the §4.3 fallback plus a SUPERSEDED entry moving that line to "when Team lands". | D11 (Thu 1 Oct) |
| 2 | Agent identity | **A bot account** (a GitHub machine user; you pick the name) with a fine-grained token for this repo only, expiring in 90 days. Log `codekid69` out of `gh` and the Keychain on this Mac, and do admin work and releases in the browser with a passkey. Why: today's `gh` token is `codekid69`'s OAuth token (`gho_`, `repo workflow gist read:org`). It reaches every repo you can see, other clients' included, and PRs opened with it can't be approved by you. **Mandatory before any prod credential exists.** | before the first push |
| 3 | Branch model | **Trunk-based:** short branch → PR → `main`. Staging is an *environment* that always runs `main`, and prod runs SemVer tags (ADR-0009; spec L5655, L7053). **No long-lived `staging` branch:** with squash-only merges it drifts from `main` for good. | D10 |
| 4 | Who runs outward commands | **You** run `git init`, repo creation, the first push and the GCP runbook; the agent rules (AGENTS.md §4 provisioning, §6 Git) forbid agents doing them. Later, the bot may run a named list of `gh`/`git push` commands after you approve an AGENTS.md §4/§7 amendment, still with a go per command. | D10 |
| 5 | GCP by hand now | **Your STATE list plus guardrails:** projects, billing, APIs, budgets, log retention, `aimos-db`, database `aimos`, the `vector` check, AR `aimos`. For the fast go-live, also the `aimos-api` account and 2 secrets (`DATABASE_URL`, `JWT_SECRET`). **Everything else is reviewed OpenTofu:** KMS, GCS, WIF, the deployer, Scheduler and audit logs at S4 (D17–18); the prod data plane at S25 (D23–25). | D10 |
| 6 | Staging database | `aimos-db` in **`aimos-staging`**, on **`db-custom-1-3840`** as you said: prod-like, which the spike's throughput test needs. It is stopped nights and weekends, and the tier can change later with a restart. **No user `app`:** `gcloud sql users create` puts it in `cloudsqlsuperuser` (CREATEROLE, CREATEDB), and the roles come from `0000_bootstrap.sql` at S5 (CANON.md:75). This departs from STATE, so it needs your OK. | §5.2 step 5 |
| 7 | Boot requirements | **Required settings by role** (`api`, `worker`, `migrate`); each feature adds its own requirement when it lands (KMS at S15, OpenAI at S16/S18). Alembic `env.py` doesn't import the full settings. Why: the scaffold check (scaffold/apps/api/app/core/config.py:97–108) needs `OPENAI_API_KEY`, `SENTRY_DSN` and `KMS_KEY_URI`, none of which exist yet, so staging couldn't boot. Changes Addendum §8, so it needs a TEAM-QUESTIONS row. | S1 plan |
| 8 | Two build tracks | **Approve an ADR-0011 + AGENTS.md §7 amendment:** one conductor; an `Active` suffix per track; the next step planned while one builds; a git worktree per track with its own compose project and DB port (a Canon edit, CANON.md:65); **only Track A writes migrations**; only the conductor edits STATE. **Without it, 4 Nov can't hold** (§6.5). | D12 |
| 9 | Launch scope | **Amazon-first on 4 Nov with cuts C1–C10 (§6.6),** agreed by the client. The alternative is full scope around 18–25 Nov. | D14 (Sun 4 Oct) |
| 10 | Contract freeze | Take proposed ADRs **0006/0007/0008** to the team now. S2 freezes **only the decided modules** as `schemas-v1.0.0` on D14; the rest are additive MINORs, and breaking fixes wait for D28. | D11 |

### Needed before the step named

| # | Decision | Recommended default | By |
|---|---|---|---|
| 11 | Cross-team approvals | GitHub's ruleset **"required reviewers" rule**, which maps paths to teams with per-team counts (GitHub changelog 2026-02-17; check it on Team). It replaces the draft's 13th `review-policy` check, needs no Canon edit, and is what KNOWN-ISSUES.md:36 asked for. | S1 |
| 12 | Release PR | The **bot runs pinned git-cliff** and opens `chore(release): vX.Y.Z` through the loop. No `release-pr.yml` or release-please: PRs opened by `GITHUB_TOKEN` trigger no checks. | D25 |
| 13 | Prod approval | **You publish the GitHub Release in the browser.** It is both the approval and the trigger. GCP issues prod credentials only when your account id, a `v*` tag, `deploy-prod.yml` and run attempt 1 all match (§4.6). | S25 |
| 14 | When prod exists | **S25 moves to D23–25.** At least 2 real `v0.x` prod releases plus a rollback drill before D43. **No `-rc` tags to prod**: the "dry run" is a real `v0.9.x`. | D20 |
| 15 | KMS grants | **API: encrypt only** (it seals tokens at the OAuth callback). **Worker: encrypt + decrypt.** Anything needing plaintext (revoke, connection health) runs as a worker job. No human gets either role. Record it as a Canon/ADR row before S4. | S4 plan |
| 16 | `BOOTSTRAP_DATABASE_URL` | **You only**, not the migrator (it is a `127.0.0.1:5433` proxy URL Cloud Run can't use anyway). Rotate the `postgres` password after `0000`. This amends CANON-ADDENDUM.md:72, so it needs a TEAM-QUESTIONS row. | S5 |
| 17 | Dependency updates | **Self-hosted Renovate, no auto-merge:** drop ADR-0005:42, which conflicts with "the user approves every merge". pip-audit, osv-scanner and Renovate send only package names and versions; record that as an AGENTS.md §1 exception. | S1 |
| 18 | Names (not in the Canon) | Services `aimos-api`, `aimos-worker`, `aimos-migrate`. Accounts `aimos-api`, `aimos-worker`, `aimos-migrator`, `aimos-deployer`, `aimos-scheduler`, `aimos-tofu`. AR `aimos`, image `backend`. **Secret id = env var name.** Workflows `ci-api.yml`, `contracts.yml`, `ci-web.yml` (CANON.md:77), plus new `ci-meta.yml`. | §5.2 step 9 |
| 19 | What goes into git | **Yes:** `docs/` (with `docs/source/`), `scaffold/`, `scripts/` (agent tooling stays local-only; see §10). **No:** `deliverables/*.docx` (39 MB of generated binaries; share them through the client's channel). | P0-U5 |
| 20 | Mutation gate | **ADR-0010 as written** (a core-module score of at least 80% blocks a release), with a written per-release waiver from you (C9). | S9 |
| 21 | Auto-rollback thresholds (ADR-0009 doesn't say) | Roll back on any of: 5xx ≥ 2% with ≥ 20 requests in a 5-minute window; p95 ≥ 2× the previous revision (floor 1.5 s); a new Sentry issue for the release; a crash loop. Traffic is too thin at v1, so add synthetic probes from a smoke tenant. | S25 |
| 22 | Second-editor subagents | Check whether the second editor loads role agents from its local config folder. If it does, AGENTS.md §7 (single-session reviews) is out of date and §7.2 idea 11 applies. | any time |

---

## 3. Phase 0: set up before coding (D9–D12)

Work top to bottom within each group. STATE records the done IDs.

### 3.1 Client: GitHub org owner [CLIENT]

- **P0-G1 [GO]** Upgrade `maniacultasset` to **Team** by D11. Set an Actions budget of about $60/mo; a
  card-billed org may default to $0, which stops CI when the free minutes run out.
- **P0-G2 [GO]** Harden the org. Require 2FA; base permission **None**. Members may **not** create
  public repos, change visibility, delete repos or invite outside collaborators (all of these are
  allowed today; checked via `gh api`). Actions: GitHub-owned actions plus an allowlist, SHA pinning if
  offered, a read-only default `GITHUB_TOKEN`, and "Actions may create/approve PRs" off. Turn on
  Dependabot alerts. Allow fine-grained tokens with approval.
- **P0-G3 [GO]** Create teams `tech-lead` (you), `backend`, `ai-eng`, `frontend`, `devops-qa`
  (CANON.md:80). Invite the AI and FE people.

### 3.2 Client: Google account [CLIENT]

- **P0-C1** A billing account in the company's name (**its currency is permanent**), and the
  Start-tier credit application (ADR-0001). The $300 trial lasts 90 days; move to paid before launch.
- **P0-C2** If there is a Workspace or Cloud Identity **organization**, the client creates both
  projects (or grants you `projectCreator` on a folder) and sets the §5.2 step 3 org policies. Without
  an org there are no org policies, and IAM goes to individuals or Google Groups.
- **P0-C3** Link both projects to billing (or grant you `billing.user`). Create the budgets (or grant
  `billing.costsManager`).
- **P0-C4** Grant **your own Google identity** (ideally a client-domain account) the staging roles
  needed for §5.2; on prod, only what "Prod now" needs. **Never run `gcloud` on this Mac as the
  client's owner:** every agent command would inherit owner rights. If you only have the client's login,
  use it once, in the browser console, to grant roles to your own identity.
- **P0-C5** Dated client items (a late item moves the step named):

| Item | By | For | If late |
|---|---|---|---|
| Apex domain + DNS (**your choice**; agents never pick one) | D12 to file, DNS live D19 | Amazon redirect URIs (CANON-ADDENDUM.md:547–550), S12 TLS | no TLS at S12; the Amazon filing waits |
| Amazon Professional seller, SP-API **private** app (Listings, Inventory, Catalog), public-app filing | now | S15, S20 | Amazon publish slips (public review takes 2–4 weeks) |
| Flipkart partner application + written sandbox question (T78) | now | S22 | cut C7 |
| Sentry org + DSN | D18 | S11 | Sentry off in staging |
| OpenAI/Azure key, prepaid for the D40 load test (the AI team picks the provider) | D27 | S16, S18 | S16/S18 slip |
| 2 launch categories (Amazon browse node, Flipkart ids) | D18 | S14a | S14a slips |
| Real CSVs (50/500/1,000 rows); brand voice + glossary; ~50 images (some bad) | D22, D24, D26 | S13, S16, S17 | synthetic fixtures |
| Email provider | D24 | invites (ADR-0006) | cut C6 |
| Pilot brand + **two** named UAT people (maker-checker needs two) | D30 | S26, gate 9 | gate 9 automated only (T8) |

### 3.3 The user

- **P0-U1** Answer decisions 1–10.
- **P0-U2** Install: Docker Desktop (Settings → "Send usage statistics" **off**, AGENTS.md §1);
  `brew analytics off`; `brew install --cask gcloud-cli`; `brew install cloud-sql-proxy libpq gitleaks
  opentofu`.
- **P0-U3** Put a **passkey** on `codekid69`. Create the bot account (with 2FA) and its fine-grained
  token: this repo only; contents, pull requests, workflows and issues read/write; actions and checks
  read; 90 days. **Then log `codekid69` out on this Mac** (`gh auth logout -u codekid69`, and delete the
  github.com item in Keychain Access), and `gh auth login --with-token` as the bot. Admin work moves to
  the browser.
- **P0-U4** Approve the agent's **tooling change pack** (P0-A3) as one change, before S1 is built.
- **P0-U5** `git init` here, add `deliverables/*.docx` to `.gitignore` (decision 19), and commit the
  baseline **after** the agent's gitleaks scan is clean (P0-A4).
- **P0-U6 [GO]** In the browser, as `codekid69`: create the **private** repo
  `maniacultasset/ai-marketplace-os` (no README); settings: squash merge only, delete branches on
  merge, no wiki or projects; add the bot as a **Write** collaborator. Then push as the bot:
  ```sh
  git remote add origin https://github.com/maniacultasset/ai-marketplace-os.git && git push -u origin main
  gh api repos/maniacultasset/ai-marketplace-os --jq .id    # record the repo id in STATE (WIF needs it)
  ```
- **P0-U7 [GO]** With Team active and P0-A3 landed: an **interim `main` ruleset** (no deletion or
  force-push, linear history, PR required, squash only, restrict updates to `tech-lead` via PR merge).
  **No required checks yet**: they don't exist, so PRs would deadlock.
- **P0-U8 [GO]** GCP runbook §5.2 steps 0–8 plus "Prod now". Step 9 runs after S1 merges.
- **P0-U9** Take ADRs 0006–0011 and TEAM-QUESTIONS to the team. From the AI team: T1 (Python 3.13) and
  **a date for the `run_listing_pipeline` drop, no later than D29**. From the FE team: confirmation
  they consume `@mkos/schemas`.
- **P0-U10** Before the S5 plan (D12): accept `DATABASE.md` rev 3 (STATE open decision 10), and OK
  the Canon PR re-planning the migration ledger (DATABASE.md:1758).

### 3.4 Agent (any editor)

- **P0-A1** Now: `plan-step S1` (docs only). S1 must be **queue-agnostic** (no Redis, no
  procrastinate yet; the ADR-0004 CI env block). It must carry the 10 checks (ADR-0012) across 4 workflows,
  the pre-commit `commit-msg` hook (§7.2 idea 8), CODEOWNERS (one pattern per line), `.gitleaks.toml`,
  role-scoped settings (decision 7) and the PR template (§7.2 idea 7).
- **P0-A2** `scripts/spec-sync.sh` before the S2 plan (the snapshot is from 2026-09-27, so it is stale
  on 1 Oct; AGENTS.md §7).
- **P0-A3** Draft the **tooling change pack**; it edits nothing until P0-U4:
  - The local conductor's merge step and its §5 by-hand table: merge becomes "push the branch, open a PR as the
    bot, CI green, the user approves and squash-merges on GitHub". It lands **before** P0-U7.
  - AGENTS.md §4/§7: the by-hand GCP exception (STATE 2026-09-29 + decision 5); the named bot commands,
    each needing a go; "agents never hold `codekid69` or GCP admin credentials"; the
    dependency-metadata egress exception.
  - The local editor settings: ask-before-run permissions for `git push*`, `git tag*`, `gh api*`, `gh pr merge*`,
    `gh release*`, `gh workflow run*`, `gh auth*`, `gcloud*`, `tofu*`, `curl*`, `security find-*`, plus
    the secret-read guard hook (§7.2 ideas 5 and 6).
  - SUPERSEDED.md: S4 `aimos-pg` → `aimos-db`; 3 projects → 2; the stale S1/S4/S12 values (arq, pg16,
    3.11, Redis, Cloud Build, gunicorn); ENV.md:35, 65, 70.
  - The by-hand mapping for all of it, in the same change (F26).
- **P0-A4** A read-only gitleaks scan of the tree (`gitleaks dir --redact .`; older versions:
  `detect --no-git`). Report the findings before P0-U5.
- **P0-A5** **ADR-0004 spike, local half** (D10–11, needs Docker): a throwaway harness outside the
  repo (e.g. `${TMPDIR:-/tmp}/mkos-spike-0004/`, deleted afterwards) against the 8 criteria (ADR-0004:59–70). Report in `docs/plans/spike-0004/REPORT.md`; it
  settles T3 before the S8 plan (D16). The staging half (criterion 6) runs after P0-U8. A failed spike
  brings Redis + ARQ back into S8/S13 only.
- **P0-A6** Draft, as proposed: **an ADR "CI/CD and release"** (§4) under the next free number (claim
  it in `docs/adr/README.md` first; 0014 at the time of writing, since ADR-0012 and ADR-0013 are
  taken and ADR-0013 already covers S1 CI on Free), and TEAM-QUESTIONS rows for decisions 15 and 16
  (decision 7 and "`JWT_SECRET` required only for the API" are now T105).
- **P0-A7** By D21: a **BUILD-SEQUENCE amendment** with S15–S26 and their "Done when" lines.
  `mkos-planner` can't plan a step without a `## S<n>` section (mkos-planner.md:20). You approve it.

---

## 4. GitHub and CI/CD

### 4.1 Repo and access

- **Repo:** `maniacultasset/ai-marketplace-os` (CANON.md:84), private for good, default branch `main`.
  Checked 2026-09-29: org id `334617292`, plan `free`, 3 seats, 0 repos; `codekid69` is a `member`, and
  members *can* create private repos (STATE.md:125–127 is stale).
- **Roles:** `tech-lead` (you, in the browser) Admin; `backend`, `ai-eng`, `frontend`, `devops-qa` Write;
  the bot Write on this repo only.
- **CODEOWNERS** (CANON.md:80): team handles, **one pattern per line**, last match wins:
  ```
  *                      @maniacultasset/tech-lead
  /apps/api/             @maniacultasset/backend @maniacultasset/tech-lead
  /apps/web/             @maniacultasset/frontend @maniacultasset/tech-lead
  /packages/pipeline/    @maniacultasset/ai-eng @maniacultasset/tech-lead
  /packages/schemas/     @maniacultasset/tech-lead @maniacultasset/backend @maniacultasset/ai-eng @maniacultasset/frontend
  /infra/                @maniacultasset/devops-qa @maniacultasset/tech-lead
  /apps/api/alembic/     @maniacultasset/tech-lead
  /scripts/db/           @maniacultasset/tech-lead
  /.github/              @maniacultasset/tech-lead
  /.gitleaks.toml        @maniacultasset/tech-lead
  /.importlinter         @maniacultasset/tech-lead
  /pyproject.toml        @maniacultasset/tech-lead
  /docs/adr/             @maniacultasset/tech-lead
  /AGENTS.md             @maniacultasset/tech-lead
  ```
AI and FE work on branches in the same repo, with PRs to `main` routed by CODEOWNERS. Only `tech-lead`
merges, which keeps "the user approves every merge". Whether their leads may merge PRs touching only
their own paths is open.

### 4.2 Rulesets (Team)

- **`main`** (no bypass): no deletion, no force-push, linear history, squash only. PR required with 1
  approval, stale approvals dismissed, code-owner review and conversations resolved. **Not** "approval
  of the most recent push" (clicking "Update branch" would void your own approval). All 10 checks
  (ADR-0012) required, **from the GitHub Actions app only**, strict, added only after a dummy PR shows
  all 10 green. Required reviewers (decision 11): `packages/schemas/**` needs 1 each from `tech-lead`, `ai-eng`
  and `frontend`; `apps/api/alembic/**` and `scripts/db/**` need `tech-lead`.
- **`main-merge`:** restrict updates; `tech-lead` may bypass only through a PR merge.
- **`release-tags`** (`v*`, `schemas-v*`): only `tech-lead` creates, updates or deletes them.
- **Environments** (Team is needed on private repos; environment *reviewers* need Enterprise, so we
  don't use them): `staging` (branch `main`), `production` (tags `v*`), `automation` (`main`; holds the
  Renovate token).

### 4.3 Fallback if the client stays on Free

Only you and the bot get Write (AI/FE use forks, or are trusted). Tags are created **only** in the
Release UI from the head of `main`. `deploy-staging.yml` starts with a `main-guard` job that refuses,
and files an issue for, any `main` commit not from a merged PR with 12 green checks (it detects; it
doesn't prevent). **Zero GitHub secrets** (on Free any branch's workflow can read them); Renovate runs
from the laptop. Reviews are routed by hand. The prod gate (§4.6) is unchanged, because GCP enforces it.

### 4.4 Workflows

**Rules:** actions pinned to full SHAs; `permissions: {}` plus a per-job minimum;
`persist-credentials: false`; **never `pull_request_target`**; the PR title and branch go through `env:`,
never inline in `run:`. A required job's id **is** its check name: no `name:`, no matrix, and no
job-level `if:`/`needs:`, because a skipped required job reports success. Path filtering happens
inside the job (`.github/actions/changed`; CANON.md:77). Telemetry is off (`DO_NOT_TRACK=1`,
`NEXT_TELEMETRY_DISABLED=1`, `CLOUDSDK_CORE_DISABLE_USAGE_REPORTING=true`). **Not used:**
`gitleaks-action` (org license key; we use the pinned binary with a sha256 check), hosted Renovate,
CodeQL and dependency review (paid on private repos; ruff `S` rules, pip-audit and osv-scanner cover
them), release-please.

| File | Trigger | Jobs (required checks in **bold**) | Budget |
|---|---|---|---|
| `ci-api.yml` | PR → `main` | **api-lint** (ruff, black, pip-audit with a dated ignore list), **api-typecheck**, **api-tests** (squawk; Alembic up → check → down → up; migration-compat; pytest), **tenant-isolation**, **lint-imports**, **bootstrap** | ≤ 9 min |
| `contracts.yml` | PR → `main` | **contracts**, **schemas-drift** | ≤ 4 min |
| `ci-web.yml` | — | none here: `web-lint` and `web-typecheck` moved to the frontend repository (ADR-0012) | — |
| `ci-meta.yml` | PR (also `edited`) | **commit-lint** (the AGENTS.md §6 regexes), **gitleaks** (over the PR range) | ≤ 1 min |
| `nightly.yml` | 02:00 IST + manual | mutmut on the core modules; full Schemathesis; deep Hypothesis; random order × 3 seeds; a migration replay with timings; **pip-audit, pnpm audit, osv-scanner and a full-history gitleaks scan, which always run, even when `main` is unchanged** | ~45 min; a failure opens a `nightly-red` issue |
| `deploy-staging.yml` | push to `main` (from S4) | §4.5 | ~18 min |
| `deploy-prod.yml` | `release: published` on `vX.Y.Z`; manual `mode=deploy\|rollback` on a tag (from S25) | §4.5 | ~25–30 min |
| `renovate.yml` | weekly + manual | self-hosted Renovate: `branchPrefix: chore/renovate-`, titles `chore(deps): …`, digests pinned, **no auto-merge** | ~5 min |

**Actions minutes:** about 9,000/month (10 PR pushes and 5 merges a day). That is about $36/mo beyond
Team's 3,000 included minutes.

### 4.5 Deploy sequence (staging and prod)

First record the current API revision and worker digest, and set `timeout-minutes`.

- **Staging:** build with buildx for `linux/amd64` (**skip it if the `sha-<sha>` tag already exists**,
  since AR tags are immutable) → push → wake the DB by running the Scheduler's start job (the deployer
  may run only that job; it never edits the instance) and wait → migrate job → API with **no traffic** under tag `r-<short>` → smoke-test the tag URL →
  shift to 100% → watch 5 min → **worker pool last**, then smoke → BRD §21 E2E → AR tag
  `staging-verified-<sha>`.
- **Prod:** guards (SemVer tag on `main`, `staging-verified` exists, Release notes present, no open
  `nightly-red` issue) → `crane copy` the digest into prod's AR and check it matches → on-demand backup
  → migrate (skipped in rollback mode) → API with no traffic → smoke → shift → **watch 15 min**
  (decision 21) → **worker last** → attach the SBOM to the Release.
- **Why the worker goes last:** a worker pool has no traffic split, and the `publish` queue sends
  listings to Amazon and Flipkart, which no rollback can undo. So there is a **job-payload
  compatibility test** next to migration-compat (the old worker must handle jobs the new API enqueues).
  Rollback runs as a step with `if: failure() || cancelled()`, and a **Cloud Monitoring 5xx alert**
  still works if GitHub dies mid-deploy.
- **Prod smoke path (open until S25):** the prod ingress `internal-and-cloud-load-balancing` also
  blocks the `run.app` tag URL from a runner. Choose (a) a load-balancer serverless-NEG URL mask with a
  `<tag>` placeholder behind IAP or a secret header (check that masks support tags), or (b) ingress
  `all` at v1. Rehearse it on staging first.
- **Migrations:** the Cloud Run job `aimos-migrate` runs on the release digest with max-retries 0,
  never at container start (CANON.md:75). The S5 wrapper `python -m app.cli db migrate` takes an
  advisory lock, sets `lock_timeout` to 5 s, and **refuses when the DB is newer than the image**
  (that's why rollback skips migrate). squawk plus migration-compat (the old code's tests on the new
  schema: `origin/main`, plus the latest `v*` once one exists) enforce expand/contract. **Strict mode
  starts at the first prod tag** (F25); before that, squawk only reports.
- **Rollback:** the fast path (~3 min) moves traffic back to the recorded revision and the worker back
  to the recorded digest. The slow path is `deploy-prod mode=rollback` on the previous tag. PITR is
  only for damaged data. Rehearsed at S25 (D25) and on D44, under 15 minutes (spec L5424).

### 4.6 Keyless GitHub → GCP (WIF), secrets, identities

- One pool `github` and one provider per project, **created by OpenTofu** (staging at S4, prod at
  S25), never by hand. **PR and nightly workflows get no binding.** The conditions use numeric ids, so
  renaming or re-creating the repo can't take over the trust (repos created after 2026-07-15 also get
  an id-based `sub`, per GitHub docs fetched 2026-09-29).
  - **Staging:**
    ```
    assertion.repository_owner_id=='334617292' && assertion.repository_id=='<REPO_ID>' && assertion.ref=='refs/heads/main'
    && assertion.event_name in ['push','workflow_dispatch'] && assertion.environment=='staging'
    && assertion.job_workflow_ref.startsWith('maniacultasset/ai-marketplace-os/.github/workflows/deploy-staging.yml@')
    ```
  - **Prod:**
    ```
    <owner and repo ids as above> && assertion.actor_id=='123632128' && assertion.run_attempt=='1'
    && assertion.ref_type=='tag' && assertion.ref.startsWith('refs/tags/v') && assertion.environment=='production'
    && assertion.event_name in ['release','workflow_dispatch']
    && assertion.job_workflow_ref.startsWith('maniacultasset/ai-marketplace-os/.github/workflows/deploy-prod.yml@refs/tags/v')
    ```
    Use `matches('^refs/tags/v[0-9]+\\.[0-9]+\\.[0-9]+$')` only if the condition language accepts it.
    `deploy-prod.yml` checks SemVer in any case.
  - `run_attempt=='1'` exists because a re-run clicked by someone else still carries your actor id. A
    failed deploy is **dispatched again**, never re-run.
- **Negative tests (S4/S25 "Done when"):** another repo, a non-`main` branch, a PR, a non-tag ref, a
  re-run and a different actor must all fail the STS exchange.
- **Your GitHub account is the prod key:** protect it with a passkey. Break-glass: the client owner
  edits the prod provider condition.
- **Deployer `aimos-deployer` per project:** `run.developer`; `iam.serviceAccountUser` **on the 3
  runtime accounts only**; AR writer on its own repo (prod also gets reader on staging's); `monitoring.viewer`;
  accessor on `SMOKE_USER` only; staging only, run on the DB start job; prod only, a custom backup role. **Never:** `logging.viewer` (logs may
  hold personal data), `cloudsql.instances.update`, KMS, app secrets, owner or editor.
- **Runtime accounts** (created at S4, except `aimos-api`, made by hand now):

| Account | Roles |
|---|---|
| `aimos-api` | `cloudsql.client`; accessor on its own secrets; **KMS encrypter** on `oauth-token-key`; `storage.objectUser` on the 2 buckets; `serviceAccountTokenCreator` on itself (V4 signed URLs without a key file); `cloudtrace.agent` |
| `aimos-worker` | the same, but **KMS encrypter/decrypter**, plus `OPENAI_API_KEY` |
| `aimos-migrator` | `cloudsql.client`; `ALEMBIC_DATABASE_URL` only |

- **Where each secret lives:**

| Where | What |
|---|---|
| GitHub | **No cloud credentials and no app secrets.** Variables only: project ids and numbers, WIF provider paths, deployer emails, region, AR paths. One secret: the Renovate token, in `automation`. |
| Secret Manager, per project, different values in each | `DATABASE_URL` (app_rw), `ALEMBIC_DATABASE_URL`, `JWT_SECRET` (different per env, ENV.md:76), `ADMIN_SESSION_SECRET`, `OPENAI_API_KEY`, `SENTRY_DSN`, `AMAZON_*`, `FLIPKART_*`, `ALERT_SLACK_WEBHOOK`, `SMOKE_USER`; `BOOTSTRAP_DATABASE_URL` for **you only** (decision 16). `NEXTAUTH_SECRET` belongs to the FE team. Rotation means a new version plus a redeploy (T98). **No vendor or marketplace secret value is stored before S4 turns on the audit logs.** |
| Laptop | `.env` from `.env.example`. The staging-only proxy URLs for S5-era migrations. **Never** a prod `app_rw` or `migrator` credential. No key files. |

- **Audit logs (S4, OpenTofu, both projects):** Data Access logs (DATA_READ, DATA_WRITE, ADMIN_READ) for
  `secretmanager`, `cloudkms`, `iamcredentials`, `sts` and `sqladmin`; a sink to a bucket kept ≥ 90
  days; alerts on a KMS Decrypt by anyone but `aimos-worker` and on any prod `setIamPolicy`. Project
  admins can still decrypt through the service accounts; these alerts are the control for that.

---

## 5. GCP

### 5.1 Summary

- **By hand now:** the decision-5 set, run by you in your own terminal, imported at S4, never
  duplicated. Everything else is reviewed OpenTofu (S4 staging, S25 prod).
- **Region `asia-south1` everywhere,** including backups (`--backup-location`) and secrets
  (user-managed replication); the defaults are multi-region.
- **Credentials on this Mac:** a dedicated gcloud config dir for this client, `gcloud auth login`
  only, **no ADC**; the proxy uses `--gcloud-auth`; delete `$CLOUDSDK_CONFIG/logs` after the bootstrap
  (it can record arguments). Agents get **no** GCP credentials: you start the proxy, and they use
  **staging** DB credentials on `127.0.0.1:5433`.
- Project ids and bucket names are global. If `aimos-staging`/`aimos-prod` is taken, **stop and ask**:
  the id is baked into Canon strings (`KMS_KEY_URI`, ENV rows).

### 5.2 Runbook (copy-paste; none of it has been run yet; the tools are not installed here)

**Step 0: workstation** (once, in your own terminal)
```bash
export CLOUDSDK_CONFIG="$HOME/.config/gcloud-aimos"          # this client only; never the default config
gcloud config set disable_usage_reporting true
gcloud auth login                                             # YOUR identity with the P0-C4 roles, never the client owner
export P=aimos-staging R=asia-south1 I=aimos-db DB=aimos
export BA=<BILLING_ACCOUNT_ID> TIER=db-custom-1-3840          # decision 6
```

**Roles the client grants your identity (P0-C4)** on `aimos-staging`: `viewer`,
`serviceusage.serviceUsageAdmin`, `cloudsql.admin`, `artifactregistry.admin`, `secretmanager.admin`,
`iam.serviceAccountAdmin`, `iam.serviceAccountUser`, `resourcemanager.projectIamAdmin`, `run.admin`,
`logging.configWriter`, `monitoring.editor`. On `aimos-prod`, only `viewer`,
`serviceusage.serviceUsageAdmin` and `logging.configWriter`. **Never:** owner on prod, any KMS
encrypt/decrypt role, `serviceAccountKeyAdmin`, `billing.admin`. After S4 these drop to `viewer`,
`cloudsql.client` and impersonation of `aimos-tofu`.

**Step 1: projects and billing** [CLIENT, or you with projectCreator and billing.user]
```bash
for X in aimos-staging aimos-prod; do
  gcloud projects create $X --name=$X --labels=app=aimos,env=${X#aimos-}   # add --organization=ORG_ID or --folder=ID if an org exists
  gcloud billing projects link $X --billing-account=$BA; done
gcloud config set project $P
```

**Step 2: APIs** (both projects; no redis, vpcaccess, servicenetworking or cloudbuild, which are
superseded; compute waits for the prod load balancer)
```bash
for X in aimos-staging aimos-prod; do gcloud services enable --project=$X serviceusage.googleapis.com \
  cloudresourcemanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  sqladmin.googleapis.com run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
  cloudkms.googleapis.com storage.googleapis.com logging.googleapis.com monitoring.googleapis.com \
  cloudtrace.googleapis.com billingbudgets.googleapis.com cloudscheduler.googleapis.com; done
```

**Step 3: guardrails** (ADR-0001 "Cost guardrails")
```bash
gcloud logging buckets update _Default --project=aimos-staging --location=global --retention-days=7
gcloud logging buckets update _Default --project=aimos-prod    --location=global --retention-days=30
gcloud projects get-iam-policy $P --format=json | grep -n "compute@developer" || true   # if it holds roles/editor, remove that binding
```
- **Org policies, if an org exists** [CLIENT]:
  - **Enforce:** `gcp.resourceLocations` = `in:asia-south1-locations` (plus global services),
    `iam.disableServiceAccountKeyCreation`, `storage.publicAccessPrevention`,
    `sql.restrictAuthorizedNetworks` and `iam.automaticIamGrantsForDefaultServiceAccounts`.
  - **Don't** enforce `sql.restrictPublicIp`: ADR-0001 uses a public IP with **no** authorized networks.

**Step 4: budgets** (50/80/100%). The amount is in the billing account's currency; for INR, about
₹5,100 for staging and ₹21,250 for prod.
```bash
budget() { gcloud billing budgets create --billing-account=$BA --display-name="$1 monthly" --budget-amount="$2" \
  --filter-projects=projects/$(gcloud projects describe $1 --format='value(projectNumber)') \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.8 --threshold-rule=percent=1.0; }
budget aimos-staging 60USD; budget aimos-prod 250USD
```

**Step 5: Cloud SQL, staging** (ADR-0001:33, ADR-0003)
```bash
gcloud sql instances create $I --project=$P --region=$R --database-version=POSTGRES_17 --edition=enterprise \
  --tier=$TIER --availability-type=zonal --storage-type=SSD --storage-size=10GB --storage-auto-increase \
  --backup-start-time=20:30 --backup-location=$R --retained-backups-count=7 \
  --enable-point-in-time-recovery --retained-transaction-log-days=7 \
  --maintenance-window-day=SUN --maintenance-window-hour=21 --maintenance-release-channel=production \
  --database-flags=max_connections=100 --insights-config-query-insights-enabled \
  --assign-ip --connector-enforcement=REQUIRED --ssl-mode=ENCRYPTED_ONLY \
  --deletion-protection --labels=app=aimos,env=staging
```
Always pass `POSTGRES_17` (the default is now PG18). **No `log_min_duration_statement`:** it would
log bind values (personal data); Query Insights strips literals. Backups run at 02:00 IST and
maintenance on Monday 02:30 IST. On `db-f1-micro`, drop `max_connections` (its default is 25; staging
needs 18).

**Step 6: database and the `postgres` password.** Do **not** run `gcloud sql users create app`
(decision 6).
```bash
gcloud sql databases create $DB --instance=$I --project=$P
PG_PW=$(openssl rand -hex 24)
printf 'postgresql://postgres:%s@127.0.0.1:5433/%s' "$PG_PW" $DB | gcloud secrets create BOOTSTRAP_DATABASE_URL \
  --project=$P --replication-policy=user-managed --locations=$R --data-file=-     # accessor: you only (owner role); no service account
printf '%s' "$PG_PW" | pbcopy
gcloud sql users set-password postgres --instance=$I --project=$P --prompt-for-password   # paste at the prompt
pbcopy </dev/null; unset PG_PW
```

**Step 7: proxy + pgvector check** (it must be at least 0.8.2; Cloud SQL ships 0.8.5 for PG17)
```bash
cloud-sql-proxy --gcloud-auth --port 5433 "$P:$R:$I" &
umask 077; PGPASSFILE="$TMPDIR/aimos.pgpass"; export PGPASSFILE
printf '127.0.0.1:5433:%s:postgres:%s\n' $DB "$(gcloud secrets versions access latest --secret=BOOTSTRAP_DATABASE_URL --project=$P | sed -E 's#^[^:]*://postgres:([^@]*)@.*#\1#')" > "$PGPASSFILE"
"$(brew --prefix libpq)/bin/psql" "host=127.0.0.1 port=5433 dbname=$DB user=postgres" -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;                          -- 0000_bootstrap.sql repeats this with IF NOT EXISTS
SELECT extversion FROM pg_extension WHERE extname='vector';     -- must be >= 0.8.2
SHOW max_connections;
SQL
rm -f "$PGPASSFILE"; unset PGPASSFILE
```
If the version is below 0.8.2: `gcloud sql instances patch $I --maintenance-version=<newest>`, then
`ALTER EXTENSION vector UPDATE;`.

**Step 8: Artifact Registry**
```bash
gcloud artifacts repositories create aimos --project=$P --location=$R --repository-format=docker --immutable-tags
cat > "$TMPDIR/ar.json" <<'J'
[{"name":"keep-releases","action":{"type":"Keep"},"condition":{"tagState":"tagged","tagPrefixes":["v","staging-verified-"]}},
 {"name":"keep-recent","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":20}},
 {"name":"drop-untagged","action":{"type":"Delete"},"condition":{"tagState":"untagged","olderThan":"14d"}},
 {"name":"drop-old-sha","action":{"type":"Delete"},"condition":{"tagState":"tagged","tagPrefixes":["sha-"],"olderThan":"30d"}}]
J
gcloud artifacts repositories set-cleanup-policies aimos --project=$P --location=$R --policy="$TMPDIR/ar.json" --no-dry-run
```
The digest currently being served always stays inside "keep-recent" or carries a `v`/`staging-verified-`
tag.

**Step 9: fast go-live** (after S1 merges; decision 5). Every value is generated; none is typed into
the shell.
```bash
gcloud iam service-accounts create aimos-api --project=$P --display-name="aimos API runtime"
API=aimos-api@$P.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding $P --member=serviceAccount:$API --role=roles/cloudsql.client --condition=None
for S in JWT_SECRET DATABASE_URL; do gcloud secrets create $S --project=$P --replication-policy=user-managed --locations=$R --labels=app=aimos; done
openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add JWT_SECRET --project=$P --data-file=-
printf 'postgresql+psycopg://app_rw:%s@/%s?host=/cloudsql/%s' "$(openssl rand -hex 24)" $DB "$P:$R:$I" \
  | gcloud secrets versions add DATABASE_URL --project=$P --data-file=-     # S5 creates app_rw with this password
for S in JWT_SECRET DATABASE_URL; do gcloud secrets add-iam-policy-binding $S --project=$P \
  --member=serviceAccount:$API --role=roles/secretmanager.secretAccessor; done
gcloud auth configure-docker $R-docker.pkg.dev
SHA=$(git rev-parse --short=12 HEAD); IMG=$R-docker.pkg.dev/$P/aimos/backend
docker buildx build --platform linux/amd64 --provenance=false --sbom=false -f apps/api/Dockerfile -t $IMG:sha-$SHA --push .   # paths as S1 defines them
DIGEST=$(gcloud artifacts docker images describe $IMG:sha-$SHA --format='value(image_summary.digest)')
gcloud run deploy aimos-api --project=$P --region=$R --image=$IMG@$DIGEST --service-account=$API \
  --add-cloudsql-instances=$P:$R:$I --cpu=1 --memory=1Gi --min-instances=0 --max-instances=1 \
  --execution-environment=gen2 --cpu-boost --ingress=all --no-allow-unauthenticated \
  --set-env-vars=APP_ENV=staging,GIT_SHA=$SHA,GCP_PROJECT_ID=$P \
  --set-secrets=DATABASE_URL=DATABASE_URL:latest,JWT_SECRET=JWT_SECRET:latest
gcloud run services proxy aimos-api --project=$P --region=$R --port=8080 &   # then: curl -s localhost:8080/healthz
```
Expected: `/healthz` 200; `/readyz` red until S5 runs `0000`. The service stays **private** until S10
auth and S12 ingress. Never pass `PORT` (Cloud Run injects it). `--provenance=false` avoids an
attestation index; S4's CI decides the SBOM path.

**Prod now:** steps 1–4 only (project, billing, APIs, 30-day logs, $250 budget). **No data plane and no
prod admin roles for anyone before S25.** S25's OpenTofu creates `db-custom-1-3840` (30 GB,
`max_connections=100`, PITR 7 days, backups retained after deletion (check the flag), a weekly export
to a retention-locked bucket, maintenance denied 30 Oct–10 Nov), its own AR, KMS key, buckets and a
fresh `JWT_SECRET`, the API at min 1/max 3 behind the load balancer (needs the domain), and the worker.

**Stop staging off-hours** by hand until S4's scheduler: `gcloud sql instances patch aimos-db
--project=aimos-staging --activation-policy=NEVER` (evenings, weekends) and `…=ALWAYS` (mornings).
Never unlink billing to save money.

### 5.3 Local development vs the cloud DB

Tests always run on local Docker (`pgvector/pgvector:0.8.5-pg17` on 5432; `mos_test`/`mos_iso` in
CI): the Alembic down-to-base round trip is destructive. The proxy on 5433 is only for the `0000`
bootstrap, the pgvector check, the spike's staging half, `alembic upgrade head` as `migrator` before
CD exists (S5 "Done when"), and read-only debugging as `app_ro`. After that, only the migrate job
changes staging.

| Where | `DATABASE_URL` (psycopg 3) | `ALEMBIC_DATABASE_URL` |
|---|---|---|
| Local Docker | `postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os` | same |
| Laptop via the proxy (staging only) | `postgresql+psycopg://app_rw:<pw>@127.0.0.1:5433/aimos` | `postgresql+psycopg://migrator:<pw>@127.0.0.1:5433/aimos` |
| Cloud Run | `postgresql+psycopg://app_rw:<pw>@/aimos?host=/cloudsql/aimos-staging:asia-south1:aimos-db` | same form, as `migrator` |

**S5 inputs:** `0000` takes **pre-computed SCRAM verifiers** (no plaintext reaches the server);
`GRANT migrator TO CURRENT_USER WITH INHERIT FALSE, SET TRUE` precedes `ALTER SCHEMA … OWNER TO
migrator` (DATABASE.md §8.1); rotate the `postgres` password afterwards; proxy sessions count against
the ledger's "proxy/admin 10" (ADR-0001).

### 5.4 Cost (USD list price before 18% GST; estimates, so check them in the pricing calculator)

- **Prod (from D23): $155–230/mo** (ADR-0001: ~$170). Cloud SQL $60–75, API (min 1) $15–40, worker
  pool $55–75, global load balancer $18–25, the rest $5–15.
- **Staging: $45–65/mo** (ADR-0001: $25–35 on `f1-micro`). Cloud SQL on `custom-1-3840` costs $25–35
  on a stop schedule, or about $60 always on ($6–12 on `f1-micro`); worker $15–20 in working hours; API
  $0–3; ingress about $0 (Firebase Hosting rewrite).
- Before S4 only the staging DB costs anything. GitHub costs about $64/mo. The $300 trial covers
  roughly the first 6–8 weeks.

### 5.5 S4 OpenTofu import (only what was made by hand)

`S` = `projects/aimos-staging`, `L` = `$S/locations/asia-south1`, `SA` = `aimos-api@aimos-staging.iam.gserviceaccount.com`.

| Made by hand | OpenTofu type | Import id |
|---|---|---|
| Projects | `data "google_project"` (not managed) | — |
| APIs | `google_project_service` (`disable_on_destroy=false`) | `aimos-staging/sqladmin.googleapis.com` |
| Log retention | `google_logging_project_bucket_config` | `$S/locations/global/buckets/_Default` |
| Budgets | `google_billing_budget` (or leave client-owned) | `billingAccounts/<BA>/budgets/<ID>` |
| SQL instance, database | `google_sql_database_instance`, `google_sql_database` | `$S/instances/aimos-db`, `…/databases/aimos` |
| AR repo | `google_artifact_registry_repository` | `$L/repositories/aimos` |
| Account + project IAM | `google_service_account`, `google_project_iam_member` | `$S/serviceAccounts/$SA`; `"aimos-staging roles/cloudsql.client serviceAccount:$SA"` |
| Secrets + IAM | `google_secret_manager_secret`, `…_secret_iam_member` | `$S/secrets/JWT_SECRET`; `"$S/secrets/DATABASE_URL roles/secretmanager.secretAccessor serviceAccount:$SA"` |
| API service | `google_cloud_run_v2_service` | `$L/services/aimos-api` |
| **Never in OpenTofu** | secret versions, the `postgres` password, DB roles and extensions (they belong to `0000`) | — |

- **How:** `import {}` blocks in `infra/envs/<env>/imports.tf`. **Gate:** `tofu plan` shows **0 to add,
  0 to destroy** for the imported set; `imports.tf` is deleted in the same PR. State goes in a
  versioned `<project>-tfstate` bucket (the one sanctioned hand-made resource at S4).
- **Guards:** `prevent_destroy` + `deletion_protection` on the instance, key and buckets;
  `ignore_changes` on the Cloud Run image, `client` and `traffic` (CD owns them) and on staging's
  `activation_policy` (the scheduler owns it). The provider counts Monday = 1, so SUN = 7.
- **New at S4:** KMS `marketplace-os`/`oauth-token-key` (permanent names; never `aimos`/`marketplace-tokens`,
  KNOWN-ISSUES.md:13); the 2 buckets (CSV lifecycle 30 days, ADR-0001); the worker, migrator, deployer,
  scheduler and tofu accounts; the remaining secret containers; WIF; audit logs + sink; Scheduler
  stop/start; `deploy-staging.yml`.

---

## 6. Roadmap to launch

### 6.1 Rules for the schedule

- One approver (you), two approvals per step, each the same day. Calendar days; agents work daily.
  Steps take 1–1.5 days; the big ones 2.5–3.5.
- **Track A** owns every migration and the core chain. **Track B** owns contracts, infra,
  observability and parallel features, and never writes a migration (decision 8).
- A step starts only after its dependencies merge (F13). The one exception: S18 may start on port
  fakes, but S16 must merge before S18's final gates.
- Migrations follow the ledger: `0004_integrity` before `0005_rls`, so RLS lands in S10, not S9
  (CANON.md ledger; DATABASE.md §8.2).

### 6.2 S1–S14 (with the proposed S12/S14 splits)

| Step | Track | Days | Starts after | For the other teams |
|---|---|---|---|---|
| S1 Backend repo, toolchain, 10 CI checks (ADR-0012); **merged 2026-09-30, PR #3** | — | planned D10–12; merged D10 | Docker, git, repo, P0-A3 | checks advisory on GitHub Free (ADR-0013 §5); required once Team lands |
| *By-hand staging deploy* (§5.2 step 9) | you | after GCP billing (planned D13) | S1, P0-U8 | private `/healthz` |
| S5 Alembic + `0000_bootstrap.sql` + roles | A | D13 | S1, P0-U10 | local + `aimos-db` via the proxy |
| S2 `packages/schemas` (17 modules) | B | D13–14 | S1, EP decision 10, T1, T61 | **D14 (Sun, MAJOR window): `schemas-v1.0.0` (decided modules)** |
| S6 `0001_core_tenancy` | A | D14 | S5, the ADR-0006/0007 verdict | |
| S3 OpenAPI stubs, TS types, mock | B | D15–16 | S2 | **D16: `/api/v1` surface out; additive only until D28** |
| S7 `0002` + `0002b` HNSW | A | D15–17 | S6 | |
| S8 `0003` + `0003b` procrastinate | A | D17–18 | S7, the spike | JobEnvelope to FE |
| S4 Import + KMS, GCS, secrets, WIF, audit, Scheduler, **`deploy-staging.yml`** | B | D17–18 | S1, P0-U8, decision 15 | **auto-deploy from `main`, about D18 (Thu 8 Oct)** |
| S11 Observability spine | B | D19–20 | S8 | |
| S9 Repository guard + isolation gate + seed | A | D19–20 | S7, S8 | |
| S14a Rules Engine as a pure function + fixture suite | B | D21–22 | S2, S7, categories | a fast rule check for the AI team |
| S10 Auth, RBAC, maker-checker, `0004_integrity`, then `0005_rls` | A | D21–23 | S9, T7 | **D23: permission matrix + JWT claims** |
| S25 Prod via OpenTofu + `deploy-prod.yml` + `v0.1.0` + rollback and PITR drills | B | D23–25 | S4, domain | **gates 6 and 10 rehearsed** |
| S12 Walking skeleton over TLS | A | D24–25 | S10, S11, S4, DNS by D19 | **D25 (Thu 15 Oct)** |
| S17a Pillow checks as a pure library | B | D26 | S2 | |
| S13 Job spine + CSV import | A | D26–27 | S12, S8, S11 | |
| S14b AI seam (5 ports + fakes), `/rules/dry-run`, `/internal/admin`, ONBOARDING | A | D28–29 | S13, S3, S9, S10 | **foundation complete D29 (Mon 19 Oct); the AI drop is due** |

Moving CD into S4 and splitting S14 are **BUILD-SEQUENCE amendments** that need your OK (P0-A7).

### 6.3 New steps after S14 (they need "Done when" lines first, P0-A7)

They close the REQUIREMENTS-TRACE.md gap areas (backend/DB 285, shared 142, ops 103).

| Step | Scope | Track | Days | Starts after |
|---|---|---|---|---|
| S15 Marketplace accounts + OAuth | LWA/Flipkart connect, `oauth_states`, KMS `KeyWrapper`, token-refresh job, connection health as a worker job; T27, T57, T77 | B | D28–29 | S10, S13, S4, Amazon app |
| S16 Brand onboarding + voice vectors | brand profile, members/invites (ADR-0006), chunking + embeddings (`agent_runs`), tenant-scoped `BrandChunkRepository.search`, a cross-tenant vector test; T38, T64, T66, T90 | B | D30–31 | S10, S13, OpenAI key |
| S18 Generation (AI seam live) | `POST /listings/generate` → job → `run_listing_pipeline`; the backend is the only writer of versions, validation runs and `agent_runs`; `blocked` after 3 attempts; regenerate, PATCH, `/validate`, a span per node; T67, T79, T80, T89, T100 | A | D30–32 | S14b, **AI drop by D29**; S16 before its final gates |
| S19 Review + approval | maker-checker in the API and the DB, `review_decisions`, detail read model, audit history, `GET /issues`; T40, T88, T91 | A | D33–34 | S18 |
| S20 Adapter framework + Amazon | SP-API Listings on the sandbox, mappings for the 2 categories, parent/child, error map → ErrorReport, throttling; T45, T49, T53, T86 | B | D32–35 | S15, sandbox |
| S21 Publish, per-field retry, status sync | `POST /publish` (Idempotency-Key, 202, `publish` queue), field results, `retry-fields`, ≤ 3 submissions (T19), bounded sync (T13, T18), DLQ requeue; FakeAdapter first | A | D35–37 | S19; S20 before its final gates |
| S17b Image pipeline (minimal) | signed upload, `images` rows, stored `ImageReport`, report endpoint, lifecycle; vision as warnings (C8) | B | D36–37 | S13, S17a |
| S23 + S24 Analytics (4 metrics) + observability completion | `GET /analytics`, Sentry releases, a click-to-marketplace trace, alerts (token refresh, DLQ, errors, budget), cost view | B | D38–39 | S21, S11 |
| S22 Flipkart adapter | mapping, throttling, errors; fixtures + FakeAdapter until credentials (T78); behind a flag at launch | B | D40–41, or after launch | S15, S21 |
| S26 Launch hardening | failure injection; load test (1,000-row CSV + 20 concurrent generations); security/tenancy/secrets audit; prompt-injection test with the AI team; automated BRD walkthrough; prod smoke; production OAuth check; runbooks | A (B joins when free) | D38–41 | everything above |

### 6.4 Milestones, contract moments, gates

Beyond the dates in §1 and §6.2: **D28 (Sun 18 Oct, MAJOR window 2)** takes contract fixes from
S15–S18 (e.g. T86). **D42 (Sun 1 Nov, last MAJOR window):** feature freeze, full regression green, 0
open P0/P1 bugs. **D43 (Mon 2 Nov):** a `v0.9.x` with a real migration is live on prod, production
OAuth is checked, and **Go/No-Go runs against the 10 gates.** **D44:** rollback rehearsal, plus UAT
with 2 client people if T8 keeps it. **D45 (Wed 4 Nov):** you publish `v1.0.0`.

| Site gate (spec L6742–6808) | Built in | Evidence | Target |
|---|---|---|---|
| 1 Feature completeness | S12–S23 | automated BRD walkthrough on staging | D42 |
| 2 Security + tenancy | S9, S10 (+RLS), S15, S26 | isolation gate goes red when the filter is removed; maker-checker enforced in the API and the DB | D41 |
| 3 Reliability + retries | S8, S13, S18, S21 | failure-injection suite | D40 |
| 4 AI contract integrity | S2, S14b, S18 | contract + golden tests, `agent_runs` | D33 |
| 5 Marketplace adapters | S20, S21 (+S22 per cut C7) | per-field results on the Amazon sandbox | D38 |
| 6 Data + recovery | S25 | measured restore time (T87) | D25, then D41 |
| 7 Observability | S11, S24 | one trace view of a failed publish; cost view | D40 |
| 8 Performance | spike (D11), S26 | 1,000-row CSV + 20 concurrent jobs | D40 |
| 9 UAT | S26 (ADR-0010; T8 open) | automated walkthrough, plus 2 client people if kept | D44 |
| 10 Rollback | S25 | auto-rollback + redeploy of the previous tag | D25, then D44 |

### 6.5 Critical path, risks and an honest reading

**Critical path:** Phase 0 → S1 → S5 → S6 → S7 → S8 → S9 → S10 → S12 → S13 → S14b → S18 (+ the AI drop)
→ S19 → S21 → S26 → Go/No-Go D43.

- It is about 31 days of step work from D10. **Slack is 1–2 days** (D42, and D43 before Go/No-Go).
- A step that uses all 3 fix rounds, or one day of approval lag, uses that slack up.
- **Single track:** S1–S14 alone ends about D35, so **4 Nov cannot hold**.
- **With a 1.5× buffer** (work has overrun before: the trace took ~19M tokens, STATE session log),
  full scope lands about **18–25 Nov**. Diwali is around 8 Nov (confirm), so the client chooses by D14.

| Risk | Where it lands | Trip-wire and response |
|---|---|---|
| Team plan late | S1 can't meet "Done when"; no enforced checks | D11: go to the §4.3 fallback + a SUPERSEDED entry |
| Two tracks not approved | critical path +10 days | D12: re-baseline, or cut C1–C10 |
| Domain late → Amazon filing late | S12 TLS, S15, S20; Amazon public review takes 2–4 weeks | D12: file with the domain; the private (self-authorised) app for the pilot brand |
| AI pipeline drop after D29 | S18 → everything after it | D28 checkpoint: a stub pipeline stays in, and the launch uses the AI team's latest drop |
| Spike fails | S8 + S13 (+Redis, Memorystore, ~+1.5 days) | D11 result; S1 is queue-agnostic |
| ADRs 0006–0008 undecided | the S2 freeze narrows | freeze only what's decided (decision 10) |
| Flipkart has no sandbox | gate 5 for Flipkart | cut C7 |
| Agents' environment leaks credentials | everything | decision 2 + P0-U3 before the first push |

**Sunday checkpoints:** **D14** S1–S6 merged, schemas frozen, spike decided, private staging up;
**scope decision**. **D21** S7–S11 + S4 merged and CD live; if S10 or CD slips, take C4 and C6.
**D28** foundation nearly done, S15 started, AI drop confirmed; if not, take C1–C3, C5, C7 and offer
the re-baseline. **D35** S19 merged and S20 publishing on the sandbox; if not, an Amazon-only pilot or a new date.

### 6.6 Cuts that keep 4 Nov

Each needs the client's OK (T16, VISION Q25). **None of them touches the 10 invariants.**

**C1** analytics = the plan's 4 metrics; the 10 BRD metrics in Phase 1.1 (T14). **C2** "ask one
question, then resume" becomes `blocked` + ErrorReport + manual edit/regenerate (the tables still ship).
**C3** `GET /api/v1/errors` analytics → 1.1; `GET /issues` ships. **C4** no batch approve/publish (T42).
**C5** no approved-listing memory (T93). **C6** no email provider by D24 → owners seeded by the admin
CLI, invites → 1.1. **C7** Flipkart behind a flag: fixtures + FakeAdapter + one supervised publish
(T78). **C8** vision results are warnings; Pillow checks ship. **C9** mutation-score waiver per release
(decision 20). **C10** BRD detail gaps (multi-GSTIN, size charts…) → 1.1.

**Team questions by step:** S2/S5: T1, T3, T7, T23, T58, T59, T61 · D14 freeze: T12, T21, T50, T60,
T83, T84, T86, T89, T100 · S14a: T9, T16, T32, T36, T48, T55 · S18–S21: T13, T18, T19, T40, T46, T47,
T51, T67, T88, T91 · S20/S22: T45, T49, T53, T77, T78 · S26: T8, T81, T82, T87.

---

## 7. Third-party agent add-on: verdict (evaluated 2026-09-29)

### 7.1 Verdict: don't install it. Copy about 8 ideas by hand.

Evidence (G = the downloaded source, kept outside the repo):
- **It phones home with no off switch.** At every session start, in every editor, a hook
  spawns a detached `npm view` of its own package version (G/hooks/hooks.json:7 → its update-check
  hook script :72–84 → its version-check script :102). There is no
  opt-out, and a reinstall restores the hook (G/bin/install.js:1372–1375). It installs unpinned via
  `npx …@latest` and needs Node ≥ 24 (G/package.json:59). **That fails AGENTS.md §1.**
- **It leaks content.** With no flags, its review command sends the plans and requirements to every
  third-party AI reviewer CLI it finds (G/docs/COMMANDS.md:1824, 1851).
  Its profile command reads the editor's per-project transcript folder in the home directory, which
  holds other clients' transcripts
  (G/src/profile-pipeline.cts:98).
- **It creates a second source of truth.** `.planning/` STATE, ROADMAP and REQUIREMENTS duplicate our
  STATE, BUILD-SEQUENCE, REQUIREMENTS-TRACE and `docs/plans/`. Its `CONTEXT.md` means something else.
  Its new-project command writes a second, editor-specific rules file.
- **It conflicts with our approvals and git rules.** It has auto-advance and auto-approval in auto
  mode (`mode: yolo` is opt-in; the default is `interactive`, G/docs/CONFIGURATION.md:220). It commits
  once per task, commits `.planning/` by default (:659), and its ship command pushes. Ours: agents never
  approve, and never push or merge without the user (AGENTS.md §6).
- **Its footprint is large:** 35 roles, 72 commands, 28 hooks and about 12k tokens of skill text per
  session. A global install changes both editors' global config for every client on this Mac.

### 7.2 Ideas to port

They land as **one** tooling change, with the conductor's §5 by-hand mapping updated in the same change, before S1
or between steps, **never mid-step**.

| # | Idea | Where | When |
|---|---|---|---|
| 5 | **Secret-read guard:** a pre-tool-call hook denying `.env*` (except `.env.example`), `*.pem`, `*-key.json`, `$CLOUDSDK_CONFIG/**`. Defense in depth only; decision 2 is the real control. | local editor hooks + settings; an AGENTS.md line for editors without hooks | P0-A3, before gcloud |
| 6 | **Ask before irreversible actions** (ask-before-run permissions; the local editor settings have none today) | local editor settings | P0-A3 |
| 7 | **PR body from REPORT.md:** summary, Done-when + trace ids, 10 gates with evidence, ADRs, risk/rollback, TDD audit | `.github/pull_request_template.md`; the conductor's step 5 | S1 |
| 8 | **Commit-message check** as a pre-commit `commit-msg` hook (not `core.hooksPath`, which makes `pre-commit install` refuse) | `.pre-commit-config.yaml` | S1 |
| 4 | **Stall detection:** open high findings don't drop between rounds → `blocked` early | the build workflow; plans README rule 5 | before S2 |
| 3 | **Validation map:** per criterion, test path, command, "fails when the guard is removed", runtime; the critic blocks untested criteria | mkos-code-planner, mkos-code-reviewer | before S2 |
| 1, 2 | **Fresh context per task group** (~3 tasks with `files`/`depends_on`, one implementer each, in sequence) + a mid-step **`HANDOFF.md`** (done, next, gates, touched files, resume step for both tools) | code-planner, implementer, the build workflow, plans README, the conductor's §5 | before S5 |
| 9, 10 | "Must be true" wiring checks (e.g. every repository subclasses the tenant base); `verify: backstop` criteria that need a held-out human test | planner, e2e-tester, verifier | by S9 |
| 11–13 | Second-editor role agents (only if decision 22 says yes); a plan index + archive; dependency provenance (PyPI URL, pin, reason) | scripts, `docs/plans/INDEX.md`, roles | low priority |

---

## 8. How the agents use this plan

| What | Where (files beat memory, AGENTS.md §7) |
|---|---|
| Rules | `docs/RULES.md` (engineering rules, tracked); `AGENTS.md` (the local agent rules) |
| Status: `Active`, `Branch`, done `P0-*` ids, current step, decisions | `docs/STATE.md`; only the launching/conductor session edits it |
| The plan | this file; changed only with the user's OK, plus a line in §10 |
| One step's loop | `docs/plans/<id>/` (CONTEXT, PLAN, CODE-PLAN line 1 = approval, LOOP, REPORT, later HANDOFF) |
| Questions and rulings | TEAM-QUESTIONS, `docs/adr/`, CANON + ADDENDUM |

**Picking up at any point:**
1. Read the front door (`docs/PRD.md`, `ARCH.md`, `RULES.md`, `DESIGN.md`, `PHASES.md`), then STATE;
   agents also read the local AGENTS.md. If `Active` isn't `none`, follow AGENTS.md §7.
2. Phase 0 open? Take the first `P0-*` id that STATE doesn't list as done. **Agents do only `P0-A*`;**
   for the others, remind the user in one line with the command.
3. Step in progress? Resume from the last block of `docs/plans/<id>/LOOP.md` (and `HANDOFF.md`).
   With the conductor: `drive <range>`; without workflows: the conductor's §5 by hand.
4. Next step: §6.2/§6.3 order, with every "Starts after" merged. In two-track mode only the conductor
   starts steps, and each `Active` suffix names its worktree.

**Outward actions, the same for every editor:** until the bot account exists (decision 2), agents
push branches and open PRs as `codekid69`, with the user's go per push or PR command (§10). The user
approves and squash-merges on GitHub. Agents never run `gcloud`, `tofu plan/apply`, `git init` or
repo creation, never touch rulesets, tags, releases or prod, and never force-push or rewrite history.
An editor without deny rules relies on the AGENTS.md text as its control. **If plan and reality
diverge,** don't improvise: add a TEAM-QUESTIONS row or a STATE open decision, ask, then add the dated
line in §10.

---

## 9. Critique log

F = feasibility critique, S = security/DevOps critique. **Every critique item not listed here was
accepted as written and is applied above.** "Checked" means verified in the repo or with `gh` on
2026-09-29.

| Item | Verdict | Reason / change |
|---|---|---|
| F2 Two tracks break the loop | Accepted | Checked AGENTS.md:238–257 (one `Active`) and the conductor's steps 5–7 (sequential): the roadmap draft's "planning while building is allowed today" was wrong. Now decision 8. |
| F3 / S1 Agents hold the prod key | Accepted (core) | Checked: the token is `gho_` OAuth, not "classic" as the GitHub draft said. A separate macOS user or devcontainer is **recommended, not mandated**: logging `codekid69` out of gh and the Keychain, plus a separate gcloud config with no ADC, removes the credentials without a second workstation. |
| F6 / S9 WIF by hand | Accepted | OpenTofu at S4/S25. **Rejected:** the GitHub draft's D7 (CD right after S1); the by-hand deploy (§5.2 step 9) gives the fast go-live instead. |
| F8 / S16 `app` user, tier | Accepted | Asked as decision 6 rather than defaulted away (AGENTS.md §2, rank 0). |
| F11 Estimates unrealistic | Partly | Scope decision moved to D14; the buffered date (18–25 Nov) is stated. The best case still uses calendar days, because the date is the client's call and the weekly trip-wires expose a slip early. |
| F14 Ledger order | Accepted | Checked CANON ledger + DATABASE.md §8.2: `0005_rls` follows `0004`, so RLS moved from S9 to S10. |
| F17 13th check | Accepted | Checked the GitHub changelog: the required-reviewer rule maps paths to teams (decision 11). |
| F29 Stale facts | Accepted | Checked: members can create private repos. STATE.md:125–127 must be fixed by the conductor (this task edited only this file). |
| F30 Mutation gate | Resolved | ADR-0010 kept with a per-release waiver (decision 20). The roadmap's blanket "advisory" was rejected: it would silently weaken a user decision. |
| F32 STATE details | Partly | spec-sync (P0-A2) and `/rules/dry-run` after S10 are in. The STATE `Active` HH:MM fix is left to the conductor. |
| S7 Audit logs by hand now | Moved to S4 | A by-hand IAM-policy JSON edit is error-prone, and until S4 the only secrets are generated ones; rule: no vendor secret before S4. |
| S12 PITR drill at S5 | Moved to S25 (D25) | S5 is on the critical path with an empty DB; D25 still leaves 18 days before gate 6. |
| S21 Add-on evidence | Accepted | `yolo` is opt-in; the default is `interactive` (§7.1). |
| GCP R2 staging `db-f1-micro` | Rejected as default | Your explicit tier is kept with its cost shown; `f1-micro` stays an option. |
| GCP R8 migrator reads the bootstrap secret | Rejected | Superuser URL for you only (decision 16). |
| GCP "KMS, GCS, all accounts by hand now" | Rejected | Not needed before S4/S15, and it shrinks the import set (decision 5). |
| Roadmap "an org owner must create the repo" | Rejected | Stale; checked via `gh api`. |

## 10. Plan changes

- 2026-09-29: created (proposed).

- **2026-09-29, the user decided the repository is backend-only (ADR-0012).** Throughout this plan:
  - Read `apps/web`, `web-lint`, `web-typecheck` and `ci-web.yml` as belonging to the frontend team's
    repository.
  - The required checks here are 10, not 12.
  - `@mkos/schemas` is published to GitHub Packages rather than consumed as a pnpm workspace package.
  - The AI pipeline is pinned from the AI team's repository (T104), and CI uses a stub.

- **2026-09-29/30, the user chose local-first with an early thin deploy.**
  - §6 dates for S4, S12 and S25 now depend on GCP billing, which is not active yet. S4 can be written and
    checked locally first; who runs `tofu plan` is STATE pending user decision P10 (§8: agents don't).
  - S15, S20 and S22 are built against a fake marketplace server and `FakeAdapter` until the credentials
    exist.
  - S18 runs on the stub pipeline until the AI team's drop.
  - The next work in order is in `docs/STATE.md` "Start here" item 2.
- **2026-09-30:** S1 merged (PR #3). Its three follow-ups are STATE "Start here" 2.1.
- **2026-09-30, handoff check (corrections only; no change of intent):**
  - §2 outcomes the user decided on 2026-09-29/30 (recorded in `docs/STATE.md` "Setup decisions"): 1 =
    stay on GitHub Free, upgrade to Team before the first prod release. 2 = agents push as `codekid69`
    for now, with a go per push or PR; a bot account before any prod credential. 3 = trunk-based. 4 =
    a one-time exception: the agent ran `git init`, the first commit, `gh repo create` and the first
    push. 6 = no `gcloud`-created `app` user. 7 = ADR-0013 §1 (T105). 8 = two tracks, yes; the
    amendment is not drafted yet (STATE pending user decision P6). 17 = Renovate with no auto-merge
    (ADR-0013). 19 = yes for `docs/`, `scaffold/`, `scripts/`; the agent tooling and `docs/plans/*/`
    stay local-only (`.git/info/exclude`); generated `.docx` files stay out of git. Still open: 5, 9,
    10, 11–16, 18, 20–22.
  - Read §1 items 1–3 ("nothing is coded", 12 checks, "once Docker and git exist") and the §6.2 S1
    row with S1 merged (PR #3) and 10 checks (ADR-0012).
  - P0-A5's harness lives outside the repo; P0-A6's ADR takes the next free number; §8 "Outward
    actions" matches decision 2 as decided; §2 numbers are cited as "EP decision N"; AGENTS.md is
    cited by section, not line; editor names are replaced with neutral terms.
- **2026-09-30, verified-check fixes (corrections only; no change of intent):**
  - §1 items 1–3, 5, 6 and 10, the Status line, and the §6.2 S1 and by-hand deploy rows now state
    today's facts in place (S1 merged, 10 checks, dates waiting on billing); the read-as notes above
    stay as history.
  - The 12-check wording in §3.4 P0-A1, §4.2, §4.4 (`ci-web.yml`) and §7.2 idea 7 now reads 10 (ADR-0012).
  - The third-party add-on is no longer named: §7 is retitled "Third-party agent add-on: verdict", its
    commands are described by role, and editor-specific setting and hook names are replaced with
    neutral terms ("ask-before-run permissions", "a pre-tool-call hook", "the conductor").
