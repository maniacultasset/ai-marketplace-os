@AGENTS.md

## Claude Code only

- The shared rules are in `AGENTS.md` (imported above), and Codex reads the same file. Never put a
  project rule only here or in auto-memory. Edit `AGENTS.md` instead.
- **Conductor:** `/drive S1..S3` runs the whole team loop (`.claude/skills/drive/SKILL.md`). A
  SessionStart hook prints STATE status at startup.
- **Team workflows:**
  - `plan-step` with `{step: "S1"}` writes `docs/plans/<id>/PLAN.md` and `CODE-PLAN.md`. The user
    approves.
  - `build-step` with `{id: "s1"}` implements, tests, reviews, fixes, then writes `REPORT.md`.
  - The agents are in `.claude/agents/`. Planning can run now. Building needs the toolchain installed
    (see STATE).
- This file and `AGENTS.md` load once, at session start, and stay in context. An edit to either one
  takes effect only in a new session, in both tools. Keep both small.
