# ADR-0006: Identity and tenancy — global users, tenant memberships, invite-only onboarding

- **Status:** ⚑ **DECISION NEEDED (team).** Proposed. It records the user's chat decision of 2026-09-27; the team confirms or overrules it, and until then we build it as written. **Provisional:** it departs from the site's DDL, so the team must confirm it (TEAM-QUESTIONS).
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead)
- **Detail:** `docs/architecture/DATABASE.md`, "Identity & tenancy"

## Context

- The site's DDL puts a user inside one tenant: `users.tenant_id` with `UNIQUE (tenant_id, email)`
  (llms-full L5885–5892; Canon 0001 copies it).
- That breaks in two known ways:
  1. **Login ambiguity (KNOWN-ISSUES):** the same email in two brands means `POST /auth/login`
     can't tell which user row is meant.
  2. **Agencies and founders with several brands** need several passwords.
- The site never describes signup (0 mentions of "invite", "sign up" or "self-serve"). BRD §5
  covers brand onboarding data, not account creation.
- In MVP, one tenant is one brand (Canon D2), and `tenant_id` stays the scoping column everywhere.

## Decision

1. **Global `users`**, with `UNIQUE (lower(email))` and no `tenant_id` or role on the user row.
2. **`tenant_memberships`** (user × tenant × role: `owner | operator | reviewer | admin`) carries
   the role and the active flag. It is `UNIQUE (tenant_id, user_id)`, plus the composite anchor used
   by FKs.
3. **The JWT `tid` is the one active tenant.** Switching brands issues a new token. Every repository
   query still filters on `tenant_id` (invariant 5 is unchanged).
4. **Maker-checker FKs point at memberships** `(tenant_id, user_id)`. This means an approver from
   another tenant fails at the FK, not only in code.
5. **Invite-only onboarding in v1:**
   - An `invitations` table stores a sha256 token hash only, a TTL and one pending invite per email.
   - Staff create tenants.
   - `tenants.status` (`trial | active | suspended | offboarded`), `plan` and `trial_ends_at` are
     added now, so self-serve signup later is additive.
6. A transactional email provider is needed; it is chosen with hosting (ADR-0001 follow-up).

## Consequences

- Canon 0001 changes: `users` is unique on `lower(email)` globally, not per tenant. This is recorded
  in CANON.md "Amendments by ADR". It also fixes the login-ambiguity defect.
- The identity RLS branches (login by email, the tenant switcher) are named exceptions to invariant
  5. Each gets its own isolation test.
- A last-owner guard is needed so a tenant can't lose its final active owner.
- If the team rejects this, we fall back to the site's per-tenant users. That means one login per
  brand and the ambiguity fix moves to "login needs a tenant slug".
