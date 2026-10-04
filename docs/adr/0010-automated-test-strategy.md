# ADR-0010: Automated test strategy — four gates, quality bars, toolset

- **Status:** ⚑ **DECISION NEEDED (team).** Proposed. It records the user's chat decision of 2026-09-27; the team confirms or overrules it, and until then we build it as written. **Departs from the site's UAT launch gate**
  (TEAM-QUESTIONS T8), so it is provisional until the team confirms.
- **Amended 2026-09-30 (no change of intent):** the required checks are 10, not 12, since ADR-0012
  moved `web-lint` and `web-typecheck` to the frontend repository.
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead)

## Context

- The site makes whole-team UAT launch gate 9 (L6732–6738, L6800–6802).
- The user's position: the backend team works mostly alone and wants every kind of test automated,
  sensitive enough to catch small regressions. No manual UAT.
- The riskiest code is small and critical: tenant isolation, maker-checker, idempotency, publish
  retries, the rules engine.

## Decision

1. **Four gates:**
   - **PR (≤ ~10 min):** lint, types, unit, integration on real PG17, contract drift, squawk,
     tenant-isolation suite, pip-audit.
   - **Nightly:** mutation testing, the Schemathesis full run, Hypothesis at high example counts,
     randomised order.
   - **Staging deploy:** migration on a prod-like copy, smoke, E2E of the BRD §21 walkthrough as an
     automated test.
   - **Prod deploy:** the zero-traffic smoke and watch window (ADR-0009).
2. **Quality bars:**
   - **Core modules** (rules engine, tenant guard, maker-checker, idempotency, publish retry): 90%
     branch coverage and a nightly mutation score of at least 80%.
   - Everything else follows the Canon coverage ramp.
3. **Toolset** (exact-pinned dev dependencies, added in S1): pytest, Hypothesis, Schemathesis,
   squawk, mutmut, pytest-randomly, pytest-socket (no network in unit tests), pip-audit, plus the
   Canon gates.
4. Tests come first in every build step (`docs/RULES.md` §8). A merge needs all 10 required checks green (ADR-0012).

## Consequences

- Launch gate 9 becomes "the automated walkthrough passes on staging" unless the team insists on
  UAT.
- Nightly runs add CI minutes. Mutation testing is limited to the core modules to bound cost.
