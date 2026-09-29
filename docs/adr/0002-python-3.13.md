# ADR-0002: Python 3.13

- **Status:** accepted (user, in chat, 2026-09-27). **AI-team confirmation pending**, because their
  pipeline runs in the same interpreter.
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead); the AI lead still has to confirm.
- **Evidence:** `docs/research/stack-2026-09.md`

## Context

The Canon pins Python `==3.11.*` (Addendum §3).
- 3.11 reaches end of life on 2027-10-31, about 12 months after launch, and the ecosystem is already
  dropping it.
- 3.13 is supported until about 2029-10. It becomes security-only after 3.13.16 on 2026-10-06.
- Every dependency declares 3.13, and the full stack installs from wheels alone.
- 3.14 would add a year, but LangGraph, the AI team's core library, doesn't declare it yet (issue
  #5253).

## Decision

- `requires-python = "==3.13.*"`, `.python-version` set to `3.13`, the Docker base
  `python:3.13-slim-trixie`, and CI on 3.13.
- No 3.14-only features. UUIDv7 comes from `uuid-utils` declared directly (`<1.0`), not from the
  standard library.
- Move to 3.14 by a new ADR once langgraph declares 3.14 support.

## Consequences

- About 2 more years of support. The switch is free now because no code exists.
- The AI team must agree (open question in STATE). If they refuse, reopen this ADR; the interpreter
  must be shared.
- Interpreter bugs found after 2026-10-06 get security fixes only.
