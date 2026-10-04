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

## Amendment (accepted, 2026-10-04): two build tracks

- **Status:** accepted (user via chat, 2026-10-04; STATE "Decisions of 2026-10-04", P6;
  EXECUTION-PLAN decision 8). Two tracks start once the per-worktree port change below has merged;
  until then the loop stays single-track.
- **Why:** single-track, S1–S14 ends about D35 and 4 Nov cannot hold; without two tracks the
  critical path grows by about 10 days (EXECUTION-PLAN §6.5).

### What changes

1. **One conductor, two tracks.** A single conductor session runs both tracks, starts every step,
   and is the only session that edits `docs/STATE.md`. Role agents still never edit STATE. The
   next step may be planned while the other track builds (plans are docs-only).
2. **Track A, the database chain:** S5 → S6 → S7 → S8 → S9 → S10, then S12, S13, S14b and every
   later step that writes a migration (EXECUTION-PLAN §6.2–§6.3 "Track" column). **Only Track A
   writes migrations or claims migration-ledger ids** (CANON.md ledger; `docs/architecture/DATABASE.md`
   §8.2). The `0004_integrity` before `0005_rls` order is unchanged.
3. **Track B, everything with no migration:** S2 and S3 (contracts), S4 (OpenTofu code; `tofu
   plan`/`apply` stay with the user), S11, S14a, S17a and other pure libraries, plus the later
   Track B rows in EXECUTION-PLAN §6.3. If a Track B step finds it needs a schema change, it stops
   and the conductor moves that change into a Track A step; Track B never writes it.
4. **Active marker per track.** In two-track mode STATE carries the conductor's `Active` line plus
   one line per track, each naming its track, branch, worktree and task. The exact text is in the
   local agent rules (AGENTS.md §7); each suffix names its worktree (EXECUTION-PLAN §8).
5. **One git worktree per track.** Track A runs in the primary checkout with the default ports.
   Track B runs in a second worktree (`git worktree add`) with its own compose project name and its
   own host ports for Postgres, the MinIO API, the MinIO console and the API, so the two local
   stacks and their test databases never collide. The ports become parameters with today's values
   as defaults (`${PG_HOST_PORT:-5432}` and the like); only the host side changes, never the
   container side. Plan artifacts for both tracks live in the primary checkout's `docs/plans/`.
6. **Dependencies and merge order.** A step still starts only after its dependencies merge (F13).
   PRs merge in the order they become mergeable. After any merge to `main`, the other track's open
   branch takes the new `main` by a merge commit (never a rebase and force-push, which the git
   rules forbid), regenerates `uv.lock` with `uv lock` instead of hand-merging it, and re-runs all
   10 checks before it may merge. When both tracks touch the same file, the conductor merges the
   PR on the critical path (Track A) first unless the other is a dependency of it.
7. **When one track blocks** (3 fix rounds spent, a team question, a missing credential): its
   `Active` line records `blocked: <reason>`, and the other track carries on. The conductor batches
   both tracks' questions into one prompt. It may start the next eligible step on the free track,
   but never moves a migration step into Track B. A Track A block is reported as a critical-path
   slip against the §6.5 slack.
8. **CI and approvals per track.** Every PR from either track runs the same 10 required checks
   (ADR-0012, ADR-0013); CI runners are per job, so the tracks never share a CI database. Each
   step keeps its own plan approval. The standing merge approval (STATE, 2026-10-04) applies per PR:
   it may be squash-merged once its plan was approved, all 10 checks are green on a branch that
   contains the current `main`, and no high or medium finding is open. Each merge is reported.

### Changes this amendment needs before it can run

- **Canon (needs the user's explicit OK; not edited here):** CANON.md:65 fixes local Postgres "on
  localhost:5432 — literally the docker-compose values". It would read as the default port, with a
  second worktree overriding only the host port in its own uncommitted `.env`. The same edit
  touches CANON.md:67 (the bootstrap `curl localhost:8000`), the local values of `PORT`,
  `DATABASE_URL`, `ALEMBIC_DATABASE_URL` and `S3_ENDPOINT_URL` in `docs/canon/ENV.md`, and the
  CANON-ADDENDUM §1 compose block and §7 bootstrap curl. The `.env.example` values stay as they are.
- **Code (a small tooling change, planned and approved like any step, before the first two-track
  step):** the host ports and compose project name become parameters in `docker-compose.yml`, the
  `Makefile`, `scripts/ci/bootstrap.sh` and `scripts/ci/prove_gates.sh`; the bootstrap image tag
  gets a per-worktree suffix; `apps/api/tests/meta/test_compose.py` follows; `.gitignore` ignores
  the per-worktree settings file.
- **Docs:** the local agent rules §6–§7 (the per-track `Active` marker and worktree setup), the
  `docs/PHASES.md` step table (a Track column), and the README port list.

### Consequences of the amendment

- About 10 days come off the critical path, at the cost of a second local stack and more merge
  updates between branches.
- The user gets up to two plan approvals in flight at once; the conductor batches them.
- If the amendment is not accepted by the EXECUTION-PLAN §6.5 trip-wire, the schedule is
  re-baselined or cuts C1–C10 are taken.
