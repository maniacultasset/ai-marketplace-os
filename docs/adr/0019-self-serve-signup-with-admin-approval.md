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
   nothing is written. Every path costs one argon2 operation, and the duplicate-pending case needs
   no error code. One answer cannot tell the cases apart, but a pair can: sending the form twice
   with the same password, or the form then `POST /signup/status`, shows whether the email was
   free (the same id, or a 200 status, because the first call just created the account) or
   already taken (two fresh ids, or a 401). Any sign-up that lets the new account authenticate
   has this oracle; closing it needs email verification, which waits for ADR-0006 decision 6. The
   per-IP limit (decision 9) bounds it, and probing a free email squats it (see Consequences).
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
10. **The staff CLI does not attach an unvetted applicant.** Nothing proves that whoever
    filled the form owns the email, and they chose its password. `tenant create --owner-email` and
   `tenant add-member --email` therefore refuse (exit 4) a user who has a sign-up request and no
   membership: otherwise a stranger could register a brand's email first and receive the tenant
   staff later create for it. The admin decides the request in the queue instead; once approved,
   the user is a normal existing user for the CLI.

## Consequences

- The FE needs three screens: the sign-up form, a "waiting for approval" screen (it reads
  `POST /signup/status`; it also offers that read after a login 401), and, for platform admins,
  the request list with approve and reject (docs/handover/FRONTEND-GUIDE.md, FLOWS.md).
- Account creation no longer needs staff. Tenant creation still needs a platform admin.
- Follow-ups, not in v1: an email notification on a decision (when ADR-0006 decision 6 picks a
  provider); `redact_user()` does not yet clear `signup_requests.owner_name` and `phone`, and an
  erasure request must clear them by hand until it does; there are no anti-automation measures
  beyond the per-IP limit (no CAPTCHA); with no email verification, someone who registers a
  brand's email first holds it: the real owner's form gets the silent 202 and the status read a
  401, and staff recover it only by rejecting the squatter's request and resetting the password
  with `user set-password` before the owner signs up again with that password.

## Amendment (11 Nov 2026): a read-only status handle

A client tester found that a pending applicant had to sign in again (Google) or retype the
password to learn whether the brand was approved. Decision 3 stays: a pending applicant still has
no token, membership or tenant. Instead:

- **The handle.** Every answer that observes a pending request (the `POST /signup` 202, a pending
  `POST /signup/status`, `POST /signup/google` and the `pending` outcome of
  `POST /auth/google/complete`) carries `status_handle`: 32 random bytes in base64url (43
  characters). Only its sha256 is stored, on the request (`signup_requests.status_handle_hash`,
  with `status_handle_expires_at` 30 days on; revision `0031_signup_status_handle`). A newer
  handle for the same request replaces the older one. The 202 of the form keeps its single shape:
  a blank answer (an existing email with a wrong password, a race) carries a random handle that
  names nothing, like its random `request_id`.
- **The read.** `POST /api/v1/signup/status/poll` takes the handle in the JSON body (never in a
  URL; `status_handle` is a denied log key) and answers only `status`, `brand_name`,
  `requested_at`, `decided_at` and, when rejected, `reason`: no id, email, phone or tenant. An
  unknown, replaced or expired handle is the same 404 `NOT_FOUND`. It is read only: it neither
  signs anyone in nor changes the request. Rate limit: twice the login rate per
  `sha256(ip|handle)`, and 20 times that per `sha256(ip)`, on top of the public per-IP limit.
- **The database.** The hash is written only by the SECURITY DEFINER
  `app_set_signup_status_handle(uuid, bytea)`, for the caller's own (`app.user_id`) pending
  request; app_rw has no UPDATE on the two columns, an app_rw INSERT that sets either one is
  refused (`signup_requests_status_handle_guard`, GRD01), and the read-only roles do not read
  them. The
  poll reads through `sr_handle_select` (the row whose live hash is `app.token_hash`).
- **What it does not change.** The oracle of decision 2 is unchanged: a live handle needs the
  account's password (the form or the status read) or a Google sign-in. The form's 202 plus one
  poll tells a right password from a wrong one for an existing email, as the status read always
  did, so `POST /signup` now takes the same per-email login limit as `POST /signup/status` (per
  `sha256(ip|email)` and per email, on top of its per-IP limit), and both stay under the
  account lockout. Whoever holds the handle (the applicant's browser keeps it in its storage)
  can read the decision for 30 days; the browser drops it once it acts on the decision. No email
  on a decision yet (still waits for ADR-0006 decision 6).


## Amendment (11 Nov 2026): one question, one reply, and the sign-in after approval

Client feedback item 2: a reviewer could only approve or reject, so a request that needed one
more detail was rejected and sent again; and the approved card's next step did not say why the
applicant has to sign in once more. Decisions 2, 3, 5 and 8 and the status-handle amendment stay
as they are. In particular a pending applicant still has no token, membership or tenant, and the
status handle still signs nobody in.

- **No new status.** Asking a question keeps the request `pending`. The status vocabulary
  (`pending | approved | rejected`), `ck_signup_requests_status`, the one-pending index, the
  login's 401, the Google `pending` outcome, the staff CLI refusal of decision 10 and the backlog
  count all read the request exactly as before, and no pinned consumer meets an unknown value
  (docs/RULES.md §11). "Waiting for the applicant" is a pending request with a question and no
  reply; "back with the admin" is one with a reply.
- **The question.** `POST /admin/signup-requests/{request_id}/ask` `{question}` (platform admins,
  1-1000 characters). One question per request: the same question again answers the stored row,
  a different second question or any question on a decided request is 409
  `INVALID_STATUS_TRANSITION`, an unknown id is 404. Audited `signup.info_requested` as `staff`
  with the admin's email and the question as the reason. Approve and reject stay open
  throughout. The admin queue shows the question and the reply on the row.
- **The reply.** The poll answers `question`, `asked_at`, `reply` and `replied_at` while the
  request is pending (null once decided), and nothing else new. `POST /signup/status/reply`
  `{status_handle, reply}` stores the applicant's one reply (1-1000 characters), authorised by
  the status handle alone, in the body, never in a URL: whoever may read the request through the
  handle may answer its one question, and nothing more. The answer is the poll's shape. An
  unknown, replaced or expired handle is the poll's 404; no open question, a decided request or a
  second, different reply is 409 (the same reply again answers it). Rate limit: the login rate
  per `sha256(ip|handle)` and 20 times that per `sha256(ip)`, on top of the public per-IP limit.
  Audited `signup.info_replied` as `system`, without the reply's text.
- **The database (revision `0037_signup_info_request`).** Four nullable columns on
  `signup_requests` with `ck_signup_requests_info` (each text with its time, a reply only after
  a question, 1-1000 characters). app_rw may UPDATE only the two question columns, and only a
  platform admin reaches a row to update through `sr_admin`; `signup_requests_info_guard`
  refuses (GRD01) an insert that sets any of them, and a question written by a non-admin, on a
  request that is not pending, or over an existing question. The reply is written only by the
  SECURITY DEFINER `app_reply_signup_info(bytea, text)`, for the row whose live handle hash it
  is given. The read-only roles read none of the four columns.
- **After approval.** The poll still signs nobody in: turning a read-only handle into a session
  would make the handle a credential, outside the login's lockout, MFA and Kuinn code. The
  approved card therefore offers the one sign-in directly and says why: a Google applicant gets
  the Google button with the intended page kept, a password applicant goes to the sign-in with the
  email filled in when this page knows it (carried in the browser's history state, never in the
  URL or local storage), and the copy says that this page only shows the status and that signing
  in once opens the workspace.
- **Follow-ups.** `redact_user()` does not yet clear `info_reply` (as for `owner_name` and
  `phone` in Consequences). One round only: a second question waits for a real need.
