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
| 0012 | This repository holds the backend only; contracts published; AI pipeline pinned from its own repo | accepted (user in chat); provisional against the site (T103); pipeline home answered by ADR-0024 (T104); Amendment 1 (npm name `@maniacultasset/schemas`, FE alias `@mkos/schemas`) proposed 2026-10-05 | 2026-09-29 |
| 0013 | Toolchain, boot settings and CI for the backend-only repo on GitHub Free (S1) | accepted (user in chat) | 2026-09-29 |
| 0014 | Tenant offboarding, the two-phase purge and user erasure | ⚑ proposed (S10a, revision 0004_integrity) | 2026-10-06 |
| 0015 | Listing lifecycle and the publish ledger | ⚑ proposed (S10a, revision 0004_integrity) | 2026-10-06 |
| 0016 | The tenant switch is the one request field naming a tenant | ⚑ proposed (S10c, PLAN Decision 4) | 2026-10-06 |
| 0017 | Job retry, shutdown release and lease recovery | ⚑ proposed (S13a, PLAN Decision 5) | 2026-10-07 |
| 0018 | Marketplace token envelope encryption | ⚑ proposed (S15, PLAN D5; OQ6, OQ20) | 2026-10-07 |
| 0019 | Self-serve sign-up with admin approval (supersedes ADR-0006 decision 5 for brand onboarding) | accepted (owner-approved in chat) | 2026-10-11 |
| 0020 | Sign in and sign up with Google, always followed by Kuinn's own login code (amends RULES §4 and ADR-0005 for RS256 on Google ID tokens only) | ⚑ proposed (Google sign-in; answers T97) | 2026-10-30 |
| 0021 | MFA secrets under their own KMS key; password and lockout policy for Amazon DPP | ⚑ proposed (DPP-1; amends the CANON.md one-key line and ADR-0018 for `mfa-secret-key`) | 2026-10-27 |
| 0022 | API edge: Cloud Armor WAF, locked ingress, candidate smoke through the load balancer (amends ADR-0001 deploy step) | ⚑ proposed (DPP-2; 0020 reserved by google-signin, 0021 by DPP-1) | 2026-10-27 |
| 0023 | Trusted browsers and a narrower step-up (amends ADR-0021 §5 and §6, ADR-0020 §5) | ⚑ proposed (trusted devices; user decisions in chat 2026-10-30) | 2026-10-30 |
| 0024 | The AI team's pipeline enters the deploy image only, by commit sha and a read-only deploy key; model gateway `LLM_GATEWAY` (`direct` or `openrouter`) (amends ADR-0012 §3, ADR-0013 §5; closes T104) | ⚑ proposed (owner decisions in chat 2026-11-02) | 2026-11-02 |
| 0025 | Interim API edge on Cloudflare Free, with an origin secret checked by the app (amends ADR-0022 until Cloud Armor quota returns; replaces its DPP 1.1.1 gate meanwhile) | ⚑ proposed (EDGE-CF; owner decisions in chat 2026-11-03) | 2026-11-03 |
| 0026 | Public image copies on the Kuinn image domain (amends ADR-0001 object storage row, DATABASE.md images note, T63) | ⚑ proposed (SI1 seller intake; owner decisions in chat 2026-11-06) | 2026-11-06 |
| 0028 | Autonomous listing resolution and the automatic resend (amends invariant 1 for non-content adapter repairs on a current approval; answers T41 and VISION Q4) | ⚑ proposed (SI11; the user's resend rule in chat 2026-11-08) | 2026-11-08 |
| 0029 | Copied invite and reset links before email; platform-admin trial recovery (amends ADR-0019's 14-day trial default; answers DATABASE.md open decisions 6 and 8 for v1) | ⚑ proposed (SI5 brand-ready; Tech Lead decisions in chat 2026-11-07; 0027 held by SI2, 0028 by SI4) | 2026-11-07 |
| 0030 | Image candidates and the AI white-background main (reverses R04763 for the main image; amends the CANON.md AI-seam row with a second entry point) | ⚑ proposed (SI7 image generation; user decision in chat 2026-11-07) | 2026-11-07 |
| 0031 | Owners may approve their own listings, per brand, off by default (amends invariant 3 and ADR-0008 decision 1 for one case; `0030_owner_self_approval`) | accepted (owner decision in chat 2026-11-11) | 2026-11-11 |
