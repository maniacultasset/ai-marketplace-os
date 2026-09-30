# docs/plans/ — one folder per build step or feature

Each `<id>/` folder (e.g. `s1/`) is the hand-off record for one pass through the team loop (the local
agent rules, AGENTS.md §6). **These files, not any tool's memory, are the loop state.** Any editor can
resume from them at any point. The folders are local-only (`.git/info/exclude`); this README is the
only committed file here, and the PR description carries the scope, criteria and results.

| File | Written by | Meaning |
|---|---|---|
| `CONTEXT.md` | mkos-planner | The context pack (≤150 lines). Covers: the step's "Done when" after the SUPERSEDED/KNOWN-ISSUES corrections, every Canon/Addendum/ADR/STATE row used (as `path:line` plus the quoted cell), git mode and branch, blocking open decisions, and a map of the existing code in the touched areas. **Every other role reads this first.** |
| `PLAN.md` | mkos-planner | Scope, acceptance criteria, risks, decisions needed |
| `CODE-PLAN.md` | mkos-code-planner | Tasks, tests first, edge-case register, migrations, gates, and the **Approvals required before code** table. **Line 1 is the status line.** |
| `LOOP.md` | the build workflow, or the agent running the loop by hand | Append-only, one block per round (format below) |
| `DEVIATIONS.md` | mkos-implementer | Any departure from the approved plan, with the reason |
| `repro/` | mkos-e2e-tester | Failing reproduction tests. Not collected by pytest. A confirmed finding's repro moves into `tests/` as its regression test. |
| `REPORT.md` | the build workflow | Final status, gates, rounds, open findings, suggestions |
| `HANDOFF.md` | the launching session | Planned (EXECUTION-PLAN §7.2 ideas 1–2, before S5): mid-step notes when a session stops: done, next, gates, touched files, resume step |

## The status line (the human approval gate)

**Line 1** of `CODE-PLAN.md` is exactly one of these:
- `Status: draft`
- `Status: approved (<who> via <file|drive|chat>, YYYY-MM-DD)`

Rules:
- No other line in the file may contain the text `Status: approved`.
- Only the user approves. They edit line 1 themselves, or answer an approval question in chat, and
  the agent then records it on line 1.
- The implementer checks the line with `head -n 1` and proceeds only on `Status: approved (`.

## Approvals required before code

CODE-PLAN.md has a table with these columns: item | kind | approver(s) | recorded.
- **Kinds that need a row:**
  - a contract MINOR or MAJOR change, plus its window date;
  - a Canon row edit;
  - a migration-ledger claim;
  - a new `ErrorCode`;
  - an ADR.
- The user fills in `recorded` (yes and a date).
- The implementer won't touch `packages/schemas/**`, `docs/canon/CANON*.md` or
  `mkos_schemas/codes.py`, and won't write a migration revision, while that row is unrecorded. The
  task goes to `tasks_blocked` instead.

## Loop rules (the build workflow implements these; an editor without workflows follows them by hand)

1. **Preflight:** a fresh session (its local rules carry a current `Rules version`, not the retired v0
   rules), the plan approved, and the right branch.
2. **Implement** test-first.
   - **Red gates are fixed before any review**, with no reviewer time spent on red code.
   - An empty gate list counts as red.
3. **Each round:**
   - The tester and both reviewers run in parallel.
   - **A missing result from any of them makes the step `blocked`,** never "clean".
4. **Findings:**
   - Dedupe them by file plus problem.
   - Low-severity findings become suggestions and are never verified.
   - High and medium findings are verified. **One verifier per file** checks all of that file's
     findings together.
   - `uncertain` on a high finding counts as open. `uncertain` on a medium finding becomes a tagged
     suggestion.
5. **Fix rounds:** at most 3 in total, gate fixes included.
   - Each fix round first appends its block to LOOP.md.
   - Re-check rounds cover only the changed files and the finding locations, at medium effort.
6. **Before the report:** if nothing is open, one final gate run on the final tree.
7. **Status:**
   - `blocked` if anything is open or blocked, or a result was missing.
   - Otherwise `ready-with-skips` if any gates were skipped or any criteria went untested.
   - Otherwise `ready-for-merge-review`.
8. **STATE.md belongs to the session that launched the run.** It sets the `Active` marker before the
   run and logs and clears it afterwards. Role agents never edit STATE.md.

**LOOP.md block format:**

```
## Round <n> — <implement|gate-fix|fix|review|final> — fixes used <k>/3
Gates: <name>=<pass|fail|skipped> …
| id | source | severity | location | problem | verdict | status (open/fixed/refuted/suggestion) |
```

## Plan critique (plan-step)

- **The code critic looks for:**
  - acceptance criteria that are missing or can't be tested;
  - tasks that don't trace to any criterion;
  - wrong Canon names;
  - unsafe migrations;
  - missing edge cases or tests;
  - over-engineering.
- **The security critic looks for** missing tenant-isolation, authorization, credential,
  input-validation and abuse controls, and the tests that prove them.
- **Routing:** PLAN.md findings go to the planner and CODE-PLAN.md findings to the code planner.
  Rejections are logged in each file's "Critique log". Low-only findings skip the revision.

## Test tooling

Hypothesis, Schemathesis, squawk, mutmut, pytest-randomly, pytest-socket and pip-audit belong to
ADR-0010 (proposed) and the ADR-0013 dependency set. All are pinned in `uv.lock` since S1 except
squawk, which S5 adds. **No role adds or upgrades a dependency outside an approved plan.** If a tool isn't in `uv.lock` yet, the check
is reported as untested, with the reason.
