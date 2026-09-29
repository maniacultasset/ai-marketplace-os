# ADR-0009: Environments, release flow, zero-traffic deploy with auto-rollback

- **Status:** ⚑ **DECISION NEEDED (team).** Proposed. It records the user's chat decision of 2026-09-27; the team confirms or overrules it, and until then we build it as written.
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead)
- **Hosting:** ADR-0001 (Cloud Run supports revision traffic control)

## Context

- The site wants a staging deploy on Cloud Run with TLS by the walking skeleton (L4960–4968). It
  wants a tested rollback in under 15 minutes (L5424, L6806–6808). It also says all 10 launch gates
  must pass before production traffic (L6742).
- The team is small and bootstrapped. The backend team owns merges and prod rollouts.

## Decision

1. **Three environments:**
   - **local:** Docker Desktop, compose with PG17 + pgvector.
   - **staging:** GCP project `aimos-staging`; every merge to `main` auto-deploys.
   - **prod:** `aimos-prod`; only a SemVer tag `vX.Y.Z` deploys, after the user approves, with a
     changelog.
   - No cloud dev tier.
2. **Prod deploy sequence:**
   1. Migrate (expand-only).
   2. Deploy the new revision with **zero traffic**.
   3. Smoke-test its private revision URL.
   4. Shift traffic.
   5. Watch error rate and latency for 10–15 minutes.
   6. **Auto-rollback** traffic to the previous revision on breach.
3. **Rollback = redeploy the previous tag.** It is possible because every migration is
   backward-compatible (expand/contract), so the previous revision runs on the new schema. It is
   rehearsed before launch, targeting under 15 minutes.
4. Infrastructure is changed only through reviewed OpenTofu (AGENTS.md §4).

## Consequences

- Migrations must never break the previous release. squawk plus a "previous revision on new schema"
  test enforce it (ADR-0010).
- The spec's EC2/systemd deployment text (spec L13014–13019) is superseded, as ADR-0001 already
  covers.
