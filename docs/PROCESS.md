# PROCESS: how the team plans, builds, reviews and merges

Purpose: the working process every contributor follows, human or agent, with any editor. It covers
the roles, the team loop, approvals, the session protocol, the git flow and the local-only files.

**This is a summary.** Engineering rules live in `docs/RULES.md`, which wins over this file on any
engineering point; loop formats live in `docs/plans/README.md`; live status and decisions live in
`docs/STATE.md`. If this file disagrees with them, they win and this file is fixed in the same PR.
Each contributor's machine may also hold local agent rules and editor glue (§8); they add
editor-specific detail and never change the process described here.

## 1. Scope, confidentiality and cloud access

- **Scope:** this repo is the backend and database only (ADR-0012): the API, schema and migrations,
  workers, adapters, the Rules Engine, auth/RBAC, the contracts package `packages/schemas`, and on
  the AI seam the brand-document chunking, embedding calls, `BrandChunkRepository.search` and the
  Pillow checks. The AI pipeline and the dashboard live in their own repos (`docs/ARCH.md` §1).
- **The client is bootstrapped:** v1 must be cheap to run without weakening security, error handling
  or the path to scale.
- **Confidential client work.** For contributors and their dev tooling (the product's own
  integrations, such as the model provider, error tracking and tracing, are out of scope here):
  - never paste or upload project code or docs to third-party services;
  - never install plugins or tools that phone home, and never add dev-tool telemetry;
  - one exception (ADR-0013 §6): pip-audit and Renovate may send package names and versions, never
    code or docs, to public vulnerability and package services.
- **Documents stay in this repo folder** (`docs/`, `deliverables/`; map in `docs/README.md`).
  Diagrams render locally with `scripts/docgen/` (offline, network blocked), never with an online
  renderer. The only outside homes are the client's GitHub org and GCP projects. Delete temp copies
  made outside the folder when the work is done.
- **Cloud access:** agents hold no GCP credentials and never run `gcloud`, `tofu plan` or
  `tofu apply` (EXECUTION-PLAN §5.1, §8). Provisioning is reviewed OpenTofu only, with one by-hand
  exception run by the user (`docs/RULES.md` §2).

## 2. Sources, questions and truth rules

- **The client's spec site is the source of truth** for what we build; its snapshot is in
  `docs/source/` (cite `L<line>`). `scripts/spec-sync.sh` checks the live site and only reports.
- **Conflict order:** `docs/README.md` "When sources disagree". Rank 0 is the user's latest explicit
  instruction; step status and the next action come from `docs/STATE.md`. An ADR or Canon row that
  departs from a clear site statement stays provisional until the team confirms it in
  `docs/review/TEAM-QUESTIONS.md`.
- **Ask, don't guess** (`docs/RULES.md` §2): a site statement that is unclear, contradictory or
  unbuildable gets a TEAM-QUESTIONS row (the site `path:line`, the proposal, the default, the impact).
  Build on the default only if waiting blocks the step. Never deviate silently.
- **Before planning a step,** pull every row that touches it from `docs/review/`
  (REQUIREMENTS-TRACE, TEAM-QUESTIONS, SPEC-TRACEABILITY, VISION-ALIGNMENT, SUPERSEDED,
  KNOWN-ISSUES) into its PLAN.md.
- **Truth rules:** files beat memory. Every claim about the project cites a file (`path:line`, a
  Canon row or spec `L<line>`). If the docs don't say, write "not specified" and ask; never invent
  endpoints, columns, env vars, error codes or versions.

## 3. Roles

Each role has a local role file (`mkos-<role>.md`, §8). Every artifact goes to `docs/plans/<id>/`.

| Role | Writes | Never |
|---|---|---|
| planner | `CONTEXT.md` (the context pack, at most 150 lines, read first by every other role) and `PLAN.md` (scope, testable acceptance criteria, Canon rows, known defects, risks, decisions needed) | writes code |
| code planner | `CODE-PLAN.md`: tasks, tests first, the edge-case register, migrations, gates, and the "Approvals required before code" table; line 1 is the status line | approves its own plan |
| code reviewer | Findings with evidence and a fix: correctness, Canon fidelity, migration safety, errors, performance, maintainability. Also critiques both plans | edits files |
| security reviewer | Findings with an exploit scenario and a fix: tenant isolation, authn/authz, credentials, injection, SSRF, OWASP API Top 10, supply chain, data exposure. Also critiques both plans | edits files |
| implementer | Tests first, then code, exactly as the approved CODE-PLAN; `DEVIATIONS.md` for any departure; fixes for gate failures and confirmed findings | starts before line 1 is approved |
| e2e tester | Tests only, against PLAN.md's criteria: tenant leaks, idempotency, retries, partial failure, migrations, property and fuzz tests; failing repros in `repro/` | writes application code |
| verifier | A `confirmed`, `refuted` or `uncertain` verdict per finding, with evidence, one verifier per file | fixes anything |

Role agents never edit `docs/STATE.md`; their facts come from `CONTEXT.md` and the loop state from
`LOOP.md`. No role adds or upgrades a dependency outside an approved plan.

## 4. The team loop

The order of work is STATE "Start here" item 2. A step starts only after its dependencies merge.
Each step or feature goes through:

1. **Plan:** the planner writes `CONTEXT.md` and `PLAN.md`; the code planner writes `CODE-PLAN.md`.
2. **Critique:** the code and security reviewers critique both plans. PLAN.md findings go to the
   planner, CODE-PLAN.md findings to the code planner; rejections go in each file's "Critique log";
   low-only findings skip the revision.
3. **Plan approval:** the user sets line 1 of `CODE-PLAN.md` to `Status: approved (...)` (§5).
4. **Build, test first:** the implementer checks line 1 with `head -n 1`, writes the failing tests
   from "Done when", then the code (`docs/RULES.md` §8).
5. **Gates:** red gates are fixed before any review; an empty gate list counts as red.
6. **Tests and reviews:** the e2e tester and both reviewers run on the change. A missing result from
   any of them makes the step `blocked`, never "clean".
7. **Verifier:** findings are deduped by file plus problem. Low findings become suggestions. High and
   medium findings are verified, one verifier per file. `uncertain` on a high finding counts as open;
   on a medium finding it becomes a tagged suggestion.
8. **Fix rounds:** at most 3 in total, gate fixes included. Each round first appends its block to
   `LOOP.md`; re-checks cover only the changed files and the finding locations.
9. **Report:** with nothing open, one final gate run on the final tree, then `REPORT.md` with status
   `blocked` (anything open or missing), `ready-with-skips` (skipped gates or untested criteria) or
   `ready-for-merge-review`.
10. **PR and merge:** a PR with a plain body (`.github/pull_request_template.md`: scope, "Done when"
    results, gate evidence), all 10 required checks green, then a squash merge (§5, §7).

"Done" is `docs/RULES.md` §8: every required check that exists has passed, with fresh output, and
every skip is named. Docs-only changes say "docs-only, no gates run". The conductor (`drive`) runs
this loop across a range of steps and stops only for batched questions, the two approvals, a
blocker or the end of the range; questions are asked in the terminal, never by push notification.
Formats and loop rules: `docs/plans/README.md`; decision record: ADR-0011.

## 5. Approvals

- **Only the user approves.** The user is the Tech Lead unless STATE names someone else. An agent
  never approves on its own; an approval counts only when the user gives it explicitly, by editing
  the file or answering an approval question in chat, and is recorded as
  `Status: approved (user via <file|drive|chat>, <date>)`.
- **Plan approval is per step:** line 1 of `CODE-PLAN.md`, and no other line may contain
  `Status: approved`.
- **Approvals before code:** Canon row edits, migration-ledger claims, new `ErrorCode`s, ADRs and
  contract changes each get a row in CODE-PLAN.md's "Approvals required before code" table, and the
  user's explicit OK before any code. Contracts also need the AI and FE leads (`docs/RULES.md` §11,
  with its interim rule).
- **ADRs:** agents claim the number in `docs/adr/README.md` and write `Status: proposed`; only the
  user sets `accepted`, and STATE records it.
- **Merge approval (standing, user 2026-10-04, `docs/STATE.md` "Decisions of 2026-10-04"):** after
  the user approves a step's plan, the agent may squash-merge its PR once all 10 CI checks are green
  and no high or medium review finding is open, and reports each merge. Without that, the user
  approves and squash-merges each PR.

## 6. Session protocol

Any contributor can hand off to any other at any point; `docs/STATE.md` is the handoff.

- **Start:**
  1. Read the front door (README "Start here"): `docs/PRD.md`, `docs/ARCH.md`, `docs/RULES.md`,
     `docs/DESIGN.md`, `docs/PHASES.md`, this file, then `docs/STATE.md`.
  2. Run `git fetch --prune` and `git status`, and note the branch. Uncommitted STATE or
     EXECUTION-PLAN edits on `main` stay in place; ask where they go.
  3. Check the `**Active:**` line in STATE. If it is not `none`: when it is older than 12 hours or
     left by your own crashed session, ask the user whether to clear it; otherwise stop and ask.
  4. If your local agent rules are out of date (their `Rules version` is older than the current one),
     restart the session: rules files load once, at session start.
  5. If the spec snapshot is more than 3 days old (STATE "Spec snapshot"), run
     `scripts/spec-sync.sh` and report changed pages before planning.
- **Before the first edit:** set
  `**Active:** <editor> · <branch> · since <YYYY-MM-DD HH:MM> · <task>`. Plan-only runs name the
  current branch. One `Active` marker means one session works at a time.
- **End:** update STATE: `Last updated`, what was done, what's next, open questions, `Branch:`, and a
  tool-neutral session-log entry of at most 5 lines (keep the newest 5; move older ones to
  `docs/archive/STATE-log.md`); then set `**Active:** none`. STATE edits ride in the current work
  branch's PR (STATE P1), so the committed `Active` line is always `none`.
- **Loop runs:** the session that launches a plan, build or drive run owns this protocol for the run:
  it sets `Active: … · <plan-step|build-step|drive> <id>` before, and logs the run and clears the
  marker after.

## 7. Git flow

Branch and title patterns, hooks and PR rules: `docs/RULES.md` §10. In short:

- **Branches:** `<type>/<slug>` with type `feat` (build steps), `fix` (follow-ups), `docs`
  (doc-only work), or `chore`, `refactor`, `test`, `perf`. PR titles are conventional commits; the
  title becomes the squash commit.
- **Flow:** branch from an up-to-date `main` → commits → push (agents only on the user's go, per
  push or PR command, until the bot account exists) → PR → all 10 checks green, confirmed with
  `gh pr checks <n>` because GitHub Free enforces nothing → squash merge on GitHub (§5) →
  `git switch main && git pull --ff-only`.
- **Never:** commit, merge or push to `main` directly; merge locally; force-push or rewrite history;
  run `git init` or create repos; touch rulesets, tags, releases or prod.
- **No AI attribution:** no co-author trailers naming an AI tool and no "generated with" footers in
  commits or PRs; every tracked file, PR body and commit message is plain engineering prose that says
  "the agent", "the editor" or "contributors".
- **Checks:** `make hooks` installs the pre-commit and commit-msg hooks (`.pre-commit-config.yaml`).
  At commit time `scripts/ci/commit_lint.py` checks the message format and
  `scripts/ci/commit_attribution.py` rejects attribution trailers and footers; in CI the
  `commit-lint` check matches the PR title and branch. Before every push,
  `git log origin/main..HEAD --format=%B | grep -iE "co-authored-by|generated with"` prints nothing.

## 8. Local-only files

These stay out of git through `.git/info/exclude` (user, 2026-09-29), and nobody force-adds them:

- the local agent rules files and editor configuration folders, which hold editor-specific glue:
  how each editor loads rules, its role files (`mkos-*.md`), the conductor and the plan and build
  workflows;
- `docs/plans/<id>/`: each step's CONTEXT, PLAN, CODE-PLAN, LOOP, DEVIATIONS, `repro/` and REPORT.
  Only `docs/plans/README.md` is committed.

**Why:** the client repo carries the product, not one contributor's tooling or working notes. The
process itself is tracked here and in `docs/plans/README.md`; the PR description carries each step's
scope, criteria and results. **Consequence:** a fresh clone lacks these files. Copy them from the
machine that has them (a dated backup is kept outside the repo, STATE P14); never recreate them from
memory.

## 9. Running the loop by hand (an editor without subagents)

Follow §4 unchanged; one session plays each role in turn, reading that role's file first, and writes
the same artifacts.

1. **Plan:** as planner, write `CONTEXT.md` and `PLAN.md`; as code planner, write `CODE-PLAN.md`;
   then critique both plans as code reviewer and as security reviewer, one after the other. Route
   findings as in §4 step 2; log low findings only.
2. **Ask once:** all questions in one chat message, as numbered options with the recommended default
   first; always include "Approve the plan?" and each unrecorded "Approvals required" row. Record the
   answers in the plan files, and line 1 only on an explicit approval.
3. **Build:** as implementer, check line 1 reads `Status: approved (`, write the tests first, then the
   code; run the gates and fix failures. Then play the e2e tester, the code reviewer, the security
   reviewer and the verifier in sequence; the verifier re-checks each finding against its cited line.
4. **Log:** one `LOOP.md` block per round, noting "single-agent review"; at most 3 fix rounds; then
   `REPORT.md` with its status.
5. **Merge:** check commit attribution by hand (§7), push on the user's go, open the PR, confirm the
   10 checks, merge per §5, then update STATE per §6.

Where the editor has no conductor, the "drive" steps above are followed by hand, and each role file's
preflight checks are run by the session itself.

Source: the local agent rules (scope, the loop, the session protocol, local-only files);
`docs/plans/README.md`; `docs/RULES.md` §2, §8, §10–§12; ADR-0011; ADR-0012; ADR-0013 §6;
`docs/STATE.md` "Decisions of 2026-10-04" and P1, P14, P16; `.pre-commit-config.yaml:30-39`;
`EXECUTION-PLAN.md` §5.1, §8; the local conductor file, section "by hand".
