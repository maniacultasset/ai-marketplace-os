# ADR ledger

One numbering, claimed here before the file is written (this fixes the collision in KNOWN-ISSUES B).
An ADR that changes a Canon row is listed in CANON.md's "Amendments by ADR" section in the same
change.

| # | Title | Status | Date |
|---|---|---|---|
| 0000 | Template | — | — |
| 0001 | Hosting on GCP asia-south1, bootstrapped profile | accepted (user delegated the choice in chat) | 2026-09-27 |
| 0002 | Python 3.13 | accepted (user in chat 2026-09-27); **AI-team confirmation pending** | 2026-09-27 |
| 0003 | PostgreSQL 17 + pgvector floor | accepted (user in chat) | 2026-09-27 |
| 0004 | Job queue in Postgres (procrastinate); drop Redis; psycopg 3 driver | accepted (user in chat); gated on the spike | 2026-09-27 |
| 0005 | Dependency and process hygiene | accepted (user in chat) | 2026-09-27 |
| 0006 | Identity and tenancy: global users + tenant_memberships; invite-only onboarding | ⚑ proposed, team decides (user's chat decision); **departs from site DDL, team to confirm** | 2026-09-27 |
| 0007 | Apparel variant families | ⚑ proposed, team decides (user's chat decision) | 2026-09-27 |
| 0008 | Human approval in v1; `tenants.auto_publish` reserved (off) | ⚑ proposed, team decides (user's chat decision); BRD disagrees (T10) | 2026-09-27 |
| 0009 | Environments, release flow, zero-traffic deploy with auto-rollback | ⚑ proposed, team decides (user's chat decision) | 2026-09-27 |
| 0010 | Automated test strategy: 4 gates, quality bars, toolset | ⚑ proposed, team decides (user's chat decision); **departs from site UAT gate (T8)** | 2026-09-27 |
| 0011 | Team loop: agents, conductor, approvals | ⚑ proposed, team decides (user's chat decision) | 2026-09-27 |
| 0012 | This repository holds the backend only; contracts published; AI pipeline pinned from its own repo | accepted (user in chat); provisional against the site (T103); pipeline home pending AI team (T104); Amendment 1 (npm name `@maniacultasset/schemas`, FE alias `@mkos/schemas`) proposed 2026-10-05 | 2026-09-29 |
| 0013 | Toolchain, boot settings and CI for the backend-only repo on GitHub Free (S1) | accepted (user in chat) | 2026-09-29 |
| 0014 | Tenant offboarding, the two-phase purge and user erasure | ⚑ proposed (S10a, revision 0004_integrity) | 2026-10-06 |
| 0015 | Listing lifecycle and the publish ledger | ⚑ proposed (S10a, revision 0004_integrity) | 2026-10-06 |
| 0016 | The tenant switch is the one request field naming a tenant | ⚑ proposed (S10c, PLAN Decision 4) | 2026-10-06 |
| 0017 | Job retry, shutdown release and lease recovery | ⚑ proposed (S13a, PLAN Decision 5) | 2026-10-07 |
| 0018 | Marketplace token envelope encryption | ⚑ proposed (S15, PLAN D5; OQ6, OQ20) | 2026-10-07 |
