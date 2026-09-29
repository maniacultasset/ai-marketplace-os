---
name: drive
description: Conductor for the AI Marketplace OS team loop. Runs plan-step, gets the user's approval, runs build-step, gets merge approval, updates STATE, then moves to the next step across a range (e.g. /drive S1..S3). It stops only for batched questions, approvals and blockers. Use when the user says /drive, "drive S1", "continue the loop" or "run the team".
argument-hint: "[next | S1 | S1..S3 | <goal in words>]"
---

# /drive — the conductor

You orchestrate the team loop in AGENTS.md §6. The workflows do the work and notify this session when
they finish; you chain the phases, talk to the user only when needed, and keep `docs/STATE.md` true.

## 0. Scope, from the argument
- Empty or `next`: the next unfinished step in STATE.md "Next actions" / BUILD-SEQUENCE.
- `S4`: that step only. `S1..S3`: that range, in order (user choice 2026-09-27: ranges run
  back-to-back).
- A goal in words: pick the minimal ordered steps from BUILD-SEQUENCE.md that reach it, show the list,
  and confirm it once with AskUserQuestion.

## 1. Preconditions (check them all, report every failure together, stop if any is fatal)
- STATE `Active:` is `none` or stale per AGENTS.md §7. Then set
  `Active: claude-code · <branch|no-git> · since <now> · drive <range>`.
  - You own STATE for the whole run. Role agents never touch it.
  - If `plan-step` throws "Stale session", tell the user to restart Claude Code and stop.
- **Build** needs the toolchain installed and the hosting ADR accepted (STATE). If they're missing,
  you may still run the planning half, and you say so.
- If the Workflow tool rejects an `mkos-*` agentType, tell the user to start a new session so the
  agents get registered, then stop.

## 2. For each step in scope

1. **Plan.**
   - Run Workflow `plan-step` with `{step}`, then wait for the notification.
   - Read only: CODE-PLAN.md "Blocking questions", PLAN.md "Decisions needed", and the Status lines.
2. **Ask once, in a batch.**
   - Use AskUserQuestion, at most 4 questions per call; repeat the call if there are more.
   - Put the recommended default first in every question.
   - Always include the question "Approve the S<n> plan?" with three options: Approve / Approve
     with changes / Revise.
   - Also ask about each unrecorded row in CODE-PLAN.md's "Approvals required before code" table:
     contract changes, Canon rows, ledger claims, ErrorCodes and ADRs.
   - Record the answers in the plan files, and fill in `recorded` (yes and a date) for approved rows.
     - **Approve:** replace **line 1** of CODE-PLAN.md with
       `Status: approved (user via /drive, <date>)`. That line is the only place this text may
       appear (docs/plans/README.md).
     - **Changes:** apply them with one `mkos-code-planner` agent, then ask for approval once more.
     - **Revise:** re-run `plan-step` with the user's notes as `goal`.
   - **Never approve on the user's behalf without an explicit answer.**
3. **Build.**
   - In a git repo: create and check out the step branch
     `feat/<id>-<slug>` (AGENTS.md §6 regex).
   - Run Workflow `build-step` with `{id, branch}` and wait.
   - Handle its status:
     - `not-approved`: fix line 1 or ask the user.
     - `stale-session`: tell the user to restart.
     - `wrong-branch`: check out the right branch.
     - `implementer-failed`: inspect the tree, then ask.
     - `ready-with-skips`: show the skipped and untested items, and ask whether to accept.
4. **If the build is blocked:**
   - Summarise the open findings in 5 lines or fewer.
   - Ask the specific decisions: accept with a follow-up issue, give guidance, or stop.
   - With guidance: one `mkos-implementer` fix, then `mkos-code-reviewer` + `mkos-security-reviewer`
     on the affected files, then `mkos-verifier` on any finding.
5. **Merge.**
   - Summarise REPORT.md in 12 lines or fewer, then ask "Approve merge of S<n>?".
   - If approved and this is a git repo: squash-commit on the step branch with a conventional title
     (AGENTS.md §6), then merge to `main`. Push only if a remote exists **and** the user approved
     pushing.
   - In no-git mode: list the files in the STATE session log.
6. **Record.**
   - Update STATE.md: the step is done, what's next, decisions (dated), and a session-log line
     including the approximate token cost of the step.
   - Delete the KNOWN-ISSUES rows this step fixed.
   - Keep `Active` held while the range continues.
7. **Continue** to the next step in the range **without asking** (user choice).

## 3. Stop when
- The range is done.
- The user says stop.
- A workflow returns nothing twice in a row.
- The user chooses stop at a blocker.

On any stop: STATE is up to date and `Active: none`.

## 4. Efficiency rules
- Batch questions, and use recommended defaults so answering is one click.
- Don't paste reports into chat. Summarise and cite the path.
- Never read the big docs whole (AGENTS.md §8). The workflows already hold the detail.
- **Token cap:** none (user choice 2026-09-27). Report cost per step in the session log instead.
- **Notifications:** terminal only (user choice 2026-09-27). Never send push notifications.

## 5. In Codex (no Workflow tool, no subagents)
Follow sections 0–4 unchanged, and swap the Claude-only parts as follows. The exact routing, caps and
statuses are in `.claude/workflows/plan-step.js` and `build-step.js`; read them as the spec.

| Claude Code | Codex does it by hand |
|---|---|
| `Active: claude-code · …` | `Active: codex · <branch\|no-git> · since <now> · drive <range>` |
| Workflow `plan-step` | Play the roles in order, each from its `.claude/agents/mkos-*.md` file: **planner** writes `CONTEXT.md` and `PLAN.md`; **code-planner** writes `CODE-PLAN.md`; then critique both plans as **code-reviewer** and **security-reviewer** in plan-critique mode. PLAN/CONTEXT findings go back to the planner role, all others to the code-planner. Low findings are logged only. |
| AskUserQuestion | Ask in chat as numbered options, recommended default first, all questions in one message. |
| Workflow `build-step` | **Implementer** (preflight: line 1 of CODE-PLAN.md reads `Status: approved (`; tests first) → run the gates → fix gate failures → **e2e-tester** → **code-reviewer** → **security-reviewer** → **verifier**, which re-checks each finding against the cited line. At most 3 fix rounds, one `LOOP.md` block per round, then `REPORT.md` with status `ready-for-merge-review`, `ready-with-skips` or `blocked`. |
| Parallel reviews | Run them one after another, and write "single-agent review" in `LOOP.md` (AGENTS.md §7). |
| "Stale session" / agentType errors | Not applicable. Instead, run each role file's preflight checks yourself. |
