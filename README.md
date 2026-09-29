# AI Marketplace OS

The monorepo for AI Marketplace OS, shared by all three teams. The layout is defined in CANON.md,
"Repository / monorepo name". This folder is owned by the backend and database team; `apps/web`
belongs to the FE team and `packages/pipeline` to the AI team. It is being built from scratch here.

**Agents (Claude Code, Codex):** read `AGENTS.md` and `docs/STATE.md` only, then grep for anything
else, as AGENTS.md §8 describes.

**Humans, start with the Word docs** in `deliverables/`: "Backend & DB Architecture" and
"Database Design (reference)". Each opens with a "Decisions needed" page, and open decisions are
highlighted amber. Rebuild them with `scripts/docgen/build.sh` after editing the `.md` sources.

**Humans, read in this order:**

1. `AGENTS.md`: scope, hard rules, source-of-truth order, and the handoff protocol.
2. `docs/STATE.md`: where we are, open decisions, next actions.
3. `docs/drafts/ARCHITECTURE.md`: the product and its end-to-end flow. It is a pre-Canon draft, so
   check its values against `docs/review/SUPERSEDED.md`.
4. `docs/canon/CANON.md` and `CANON-ADDENDUM.md`: the authority. The Addendum wins over the Canon.
5. `docs/drafts/BUILD-SEQUENCE.md`: build steps S1–S14. Read it alongside `SUPERSEDED.md` and
   `KNOWN-ISSUES.md`.

## Layout (today)

```
AGENTS.md               shared agent rulebook (Codex + Claude Code)
CLAUDE.md               imports AGENTS.md, plus Claude-only notes
docs/STATE.md           live status + handoff log
docs/README.md          map of every doc: what it is, how much authority it has, when to read it
docs/EXECUTION-PLAN.md  the plan from setup to launch
docs/canon/             CANON, CANON-ADDENDUM, ENV: the authority (after the site and accepted ADRs)
docs/drafts/            pre-Canon drafts: BUILD-SEQUENCE, ARCHITECTURE, CONTRACTS, ... (intent only)
docs/review/            TEAM-QUESTIONS, REQUIREMENTS-TRACE, SPEC-TRACEABILITY, VISION, KNOWN-ISSUES, SUPERSEDED
docs/architecture/      DATABASE.md + BACKEND-ARCHITECTURE.md (the design; sources of the Word docs)
docs/adr/               architecture decision records
docs/trace/             line-by-line register of the client spec + verdicts (summary: docs/review/REQUIREMENTS-TRACE.md)
docs/archive/           retired files, not loaded
scaffold/               starter files; moved in one by one during S1 (never `cp -R`)
scripts/                spec-sync.sh; docgen/ (Word docs, offline); trace/ (spec-trace checks)
deliverables/           team Word docs (generated) + kickoff pack
```

The target layout (`apps/api`, `apps/web`, `packages/schemas`, `packages/pipeline`, `infra`,
`scripts`, `.github`) is created in build step S1.
