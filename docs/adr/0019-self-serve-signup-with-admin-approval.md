# ADR-0019: Self-serve sign-up with admin approval

- **Status:** accepted (owner-approved in chat, 2026-10-11)
- **Date:** 2026-10-11
- **Deciders:** Tech Lead (the user)
- **Supersedes:** ADR-0006 decision 5 ("invite-only onboarding in v1") for brand onboarding only.
  Invitations stay as they are for adding members to a running tenant.

## Context

ADR-0006 made v1 invite-only: staff create every tenant with `python -m app.cli tenant create`.
The owner wants a brand to sign up on its own with the minimum details, wait for a platform admin
to approve it, then log in, finish onboarding (brand profile, documents) and start working. No
email provider exists yet (ADR-0006 decision 6 is still open), so nothing may depend on email.

## Decision

1. **The public form is minimal.** `POST /api/v1/signup` takes brand name, owner name, email and
   password, plus an optional phone and website. It creates the global `users` row (argon2id, the
   login's email rules) and a `signup_requests` row with status `pending`. Everything else (tone,
   guidelines, glossary, brand documents) is filled in after approval with the existing
   `PATCH /tenants/me` and `/brand/documents`; those endpoints do not change.
2. **The answer never reveals an account.** The form always answers 202
   `{request_id, status: "pending"}`. A new email gets a user and a request. An existing email with
   the right password gets a request too (or its current pending one: re-submitting is
   idempotent). An existing email with a wrong password gets the same 202 with a fresh id, and
   nothing is written. Every path costs one argon2 operation. So the form reveals no more than the
   login already does (the login's 401 is also the same for an unknown email and a wrong
   password). Because of this, the duplicate-pending case needs no error code.
3. **A pending applicant has no token.** A pending or rejected applicant has no membership. The
   login therefore keeps its existing no-membership behaviour (the one 401), and no token, tenant
   GUC or tenant row exists for them. We chose this over a tenant-less token: a token without
   `tid` would change `JWTClaims` (a published contract), `refresh_tokens.tenant_id` (NOT NULL,
   under RLS) and every principal probe, for a screen that only needs one status read. The status
   read is `POST /api/v1/signup/status`, which takes email and password. It answers the newest
   request (`pending`, `rejected` with the admin's reason, or `approved` with the tenant id), or the
   login's 401 for any refusal. It uses the login's rate limiter.
4. **Platform admin is a flag on the user, not a role.** `users.is_platform_admin` (default false).
   It is set only by staff, with `python -m app.cli user make-admin --email E --staff-email F`
   (migrator, audited, idempotent). A trigger refuses any other writer, so neither a sign-up body
   nor app_rw can set it. A platform admin logs in like anyone else, through a membership (for
   example, a staff tenant made with `tenant create`). The admin endpoints check the flag on the
   user's own row on every request, never from the token, so revoking the flag takes effect at
   once. It is not an `Action` in the role matrix (an owner holds every action, so a matrix entry
   would grant it to every owner). It is a policy of its own, `platform:admin`. `GET /auth/me`
   exposes `is_platform_admin` so the FE shows the queue only to admins. Tokens do not carry the
   flag (`JWTClaims` is unchanged).
5. **The queue.** `GET /api/v1/admin/signup-requests` (keyset pages, oldest first, an optional
   status filter). `POST .../{request_id}/approve` (optional `trial_days`, default 14) creates the
   tenant (`trial`, a slug derived from the brand name and made unique with a random 4-hex suffix)
   and the owner membership. It uses the staff CLI's own statements (`staff_ops.insert_tenant` and
   `add_owner`), so a CLI tenant and an approved sign-up are created the same way and audited the
   same way: `tenant.created` and `membership.created` as `staff` with the admin's email, plus
   `signup.approved`. `POST .../{request_id}/reject` takes a reason (audited as
   `signup.rejected`). Both are idempotent: the same decision again answers the stored row, and
   the opposite decision is 409 `INVALID_STATUS_TRANSITION`. An unknown id is 404.
6. **Database (revision 0010_signup_requests).** `signup_requests` is a platform table with no
   `tenant_id`. The tenant it creates is `created_tenant_id`, which is not a scoping column. It
   has FORCE RLS. The applicant reads only their own rows, and inserts only a fresh pending row
   for themselves (`app.user_id`). A platform admin reads and decides every row; the check is
   `app_is_platform_admin()`, a SECURITY DEFINER function, so the policy does not re-enter the
   `users` policies. Everyone else, and a session with no GUC, sees nothing. One pending request
   per user (a partial unique index). app_rw has no DELETE and may UPDATE only the decision
   columns. The read-only roles never see `owner_name` or `phone`. `users_platform_admin` lets an
   admin read applicants' user rows (the queue shows the email).
7. **Admin writes run as app_rw, not through `platform_scope()`.** `platform_scope()` is read-only
   (`app_platform_ro`), and repo rule R5 keeps it out of services. The approval has to write (a
   tenant, a membership, audit rows), so it runs in the identity session as app_rw. The RLS above
   enforces the admin check a second time in the database.
8. **No new error codes.** Adding an `ErrorCode` member is MAJOR (docs/RULES.md §11) and would
   wait for the 18 Oct window. This flow uses existing codes only (`VALIDATION_FAILED`,
   `UNAUTHENTICATED`, `PERMISSION_DENIED`, `NOT_FOUND`, `INVALID_STATUS_TRANSITION`,
   `RATE_LIMITED`), so the contract change is an additive MINOR under the open
   `## 1.1.0 (unreleased)` heading.
9. **Rate limits.** The form is limited to 5 a minute per client IP (`signup:<sha256(ip)>`, the
   login's per-minute setting), on top of the public 60 a minute per IP. The status read uses the
   login's per-IP-and-email bucket.

## Consequences

- The FE needs three screens: the sign-up form, a "waiting for approval" screen (it reads
  `POST /signup/status`; it also offers that read after a login 401), and, for platform admins,
  the request list with approve and reject (docs/handover/FRONTEND-GUIDE.md, FLOWS.md).
- Account creation no longer needs staff. Tenant creation still needs a platform admin.
- Follow-ups, not in v1: an email notification on a decision (when ADR-0006 decision 6 picks a
  provider); `redact_user()` does not yet clear `signup_requests.owner_name` and `phone`, and an
  erasure request must clear them by hand until it does; there are no anti-automation measures
  beyond the per-IP limit (no CAPTCHA).
