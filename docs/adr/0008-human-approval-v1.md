# ADR-0008: Human approval in v1; `tenants.auto_publish` reserved and off

- **Status:** ⚑ **DECISION NEEDED (team).** Proposed. It records the user's chat decision of 2026-09-27; the team confirms or overrules it, and until then we build it as written. It follows the build plan and specs. **The BRD
  disagrees** (TEAM-QUESTIONS T10), so the client must confirm the long-term position.
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead)

## Context

- **Build plan and specs:** zero auto-publish in Phase 1 (llms-full L4700, L4812). Confidence-based
  auto-publish is a Phase 3 unlock "after proven approval track records" (L6881, L7405).
- **BRD:** autonomous submission, with approval optional (L39–41, L2243).
- Invariant 3 makes human approval mandatory in Phase 1. Maker-checker means the creator can't
  approve their own work (L4689, L5313). That conflicts with the one-person-brand DoD line (L5450,
  TEAM-QUESTIONS T15).

## Decision

1. **v1 publishes only after a human approves a specific `listing_version`.**
   - The approver is a different active member with the `reviewer` or `owner` role (T88's default
     drops `admin`, which holds no `listing:approve` in the role matrix; S19 builds it).
   - This is enforced in the API **and** by a DB trigger (the backstop).
2. **`tenants.auto_publish boolean NOT NULL DEFAULT false`** is added now, with
   `CHECK (auto_publish = false)` in v1.
   - Turning it on later needs a new ADR, removing the CHECK, and the Phase 3 confidence gate.
   - Adding the column now avoids a schema change on a hot table later.
3. Every approval writes a `review_decisions` row and an `audit_log` entry.

## Consequences

- Every brand needs at least two members (a maker and a checker). Onboarding must say so (T15).
- The publish path is unchanged when auto-publish arrives. Only the approval source changes (human
  vs policy), and it is audited the same way.
