# ADR-0011: Team loop — role agents, the `/drive` conductor, approvals

- **Status:** ⚑ **DECISION NEEDED (team).** Proposed. It records the user's chat decision of 2026-09-27; the team confirms or overrules it, and until then we build it as written.
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead)
- **Detail:** AGENTS.md §6–§7, `docs/plans/README.md`, `.claude/agents/mkos-*.md`,
  `.claude/skills/drive/SKILL.md`

## Context

- Claude Code and Codex both work in this repo and must hand over at any point without clashing
  or inventing facts.
- The user wants a senior-grade, token-efficient loop that plans, codes, tests and reviews on its
  own, and stops only for real decisions.

## Decision

1. **Per build step:**
   1. Context pack (`CONTEXT.md`).
   2. Plan, then two critics.
   3. **The user approves the plan** (written on line 1: `Status: approved (<who> via <how>, date)`).
   4. Code plan.
   5. Tests first, then code.
   6. Gates.
   7. Code and security reviews, each finding re-checked by one verifier per file.
   8. At most 3 fix rounds, then `blocked` and the user is asked.
   9. **The user approves the merge.**
2. **Roles** are `.claude/agents/mkos-*.md`: planner, code-planner, implementer, e2e-tester,
   code-reviewer, security-reviewer, verifier. Codex plays the same roles one after another.
3. **`/drive`** runs this loop across a step range (e.g. `S1..S3`).
   - It stops only for batched questions, approvals, blockers or the end of the range.
   - Prompts are terminal-only, with no token cap; cost is logged per step.
4. **`docs/STATE.md` is the handoff.**
   - Only the launching session edits it; role agents never do.
   - An Active marker prevents two tools working at once.
5. Contract changes (`packages/schemas`) need all three leads (AGENTS.md §6).

## Consequences

- Every step leaves a paper trail in `docs/plans/<step>/`: CONTEXT, PLAN, CODE-PLAN, LOOP, REPORT.
- The two approvals are the only human waits per step.
