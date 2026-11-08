# ADR-0029: Copied invite and reset links before email; platform-admin trial recovery

- **Status:** proposed (SI5 brand-ready, 2026-11-07)
- **Date:** 2026-11-07
- **Deciders:** Tech Lead (the user)
- **Amends:** ADR-0019 decision "optional `trial_days`, default 14" (`0019-self-serve-signup-with-admin-approval.md:53`).
  Answers `docs/architecture/DATABASE.md` open decisions 6 (invitation TTL; transactional email provider) and 8 (who
  can invite or promote an owner; password reset in v1) for v1. Open decision 18 (email delivery) stays open.

## Context

The first real brands need to add teammates, recover a forgotten password and survive the end of a trial without
staff help. The `invitations` table exists, but no route uses it; Kuinn has no transactional email provider yet; a
forgotten password needs the staff CLI (`python -m app.cli user set-password`); and a trial that ends suspends the
tenant with no way back except a database change. Users are global (one login, several memberships), so a password
reset changes a credential that other tenants rely on.

## Decision

1. **Copied links instead of email.** An owner or admin (`team:manage`) creates an invite and gets a link once:
   `<DASHBOARD_BASE_URL>/invite#token=<token>`. The token is 32 random bytes (base64url); only its sha256 is stored.
   An invite lives **7 days**, is single use, revocable, and a new invite for the same email revokes the old one. Only
   an owner invites an owner. The token travels in the URL fragment, so it never reaches a server log or a Referer.
2. **Accept.** A person with no account creates one with a password (the DPP rules) or with Google when the verified
   Google email equals the invite email. A person with an account signs in and joins. Every refusal (unknown, used,
   revoked, expired, tenant not trial or active, inviter no longer an active owner or admin) is one generic 404.
3. **Reset links.** An owner or admin issues a **24-hour**, single-use link
   (`<DASHBOARD_BASE_URL>/reset-password#token=<token>`) for a member. Eligibility: an active member of only this
   tenant (no membership elsewhere, whatever its status), not the issuer, not a platform admin, an owner only by an
   owner, a password account, and only while `MFA_ENFORCEMENT=all` with a confirmed TOTP factor on the target. Under
   `off` a password alone signs in, so the factor would not stop a takeover and every link is refused. Redemption
   re-checks all of it and that the issuer still holds the role; it ends every session and trusted browser and leaves
   the MFA factor and the lockout state alone. Everyone else stays with the staff CLI.
4. **Trial default 30 days.** A sign-up approval without `trial_days` gives a 30-day trial (was 14).
5. **Platform-admin recovery over HTTP.** A platform admin may activate a trial or suspended tenant and extend a trial
   by 1 to 90 days, or return a tenant suspended by trial expiry to `trial`. Both need a reason, run through two new
   SECURITY DEFINER functions (`activate_tenant`, `extend_tenant_trial`) and are audited as staff with the admin's
   email. Suspend and offboard stay with the staff CLI. The CLI gains `tenant activate` and `tenant extend-trial`.

## Consequences

- Brands invite and recover teammates the same day, with no email provider and no staff ticket.
- A link is a bearer secret until it is used or expires. Its holder can create the invited account or set the
  member's password; the TTLs, single use, revocation and the target's TOTP bound that risk (KNOWN-ISSUES).
- When an email provider arrives, the same tokens go into the email body; the tables and routes do not change.
- Owners of several brands, platform admins and members without a confirmed factor still need the staff CLI.
