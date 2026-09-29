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
