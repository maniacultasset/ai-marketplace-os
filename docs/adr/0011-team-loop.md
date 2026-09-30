# ADR-0011: Team loop — role agents, the `/drive` conductor, approvals

- **Status:** ⚑ **DECISION NEEDED (team).** Proposed. It records the user's chat decision of 2026-09-27; the team confirms or overrules it, and until then we build it as written.
- **Amended 2026-09-30 (wording, no change of intent):** step order aligned with `docs/plans/README.md`;
  editor names made neutral.
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead)
- **Detail:** the local agent rules (AGENTS.md §6–§7), `docs/plans/README.md`, and the local role
  files and conductor (local-only, `.git/info/exclude`)

## Context

- Two coding-agent editors work in this repo and must hand over at any point without clashing or
  inventing facts.
- The user wants a senior-grade, token-efficient loop that plans, codes, tests and reviews on its
  own, and stops only for real decisions.

## Decision

1. **Per build step:**
   1. Context pack (`CONTEXT.md`).
   2. Plan (`PLAN.md`), then code plan (`CODE-PLAN.md`).
   3. Two critics (code, security) review both plans.
   4. **The user approves the plan** (line 1 of `CODE-PLAN.md`: `Status: approved (<who> via <how>, date)`).
   5. Tests first, then code.
   6. Gates.
   7. Code and security reviews, each finding re-checked by one verifier per file.
   8. At most 3 fix rounds, then `blocked` and the user is asked.
   9. **The user approves the merge.**
2. **Roles** are the local role files `mkos-*.md`: planner, code-planner, implementer, e2e-tester,
   code-reviewer, security-reviewer, verifier. An editor without subagents plays the same roles one
   after another.
3. **The loop conductor** (`drive`) runs this loop across a step range (e.g. `S1..S3`).
   - It stops only for batched questions, approvals, blockers or the end of the range.
   - Prompts are terminal-only, with no token cap; cost is logged per step.
4. **`docs/STATE.md` is the handoff.**
   - Only the launching session edits it; role agents never do.
   - An Active marker prevents two editors working at once.
5. Contract changes (`packages/schemas`) need all three leads (AGENTS.md §6).

## Consequences

- Every step leaves a paper trail in `docs/plans/<step>/`: CONTEXT, PLAN, CODE-PLAN, LOOP, REPORT.
- The two approvals are the only human waits per step.
