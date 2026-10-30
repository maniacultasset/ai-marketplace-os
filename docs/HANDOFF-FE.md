# Handoff to the FE team: the v1 API

> **Superseded for v1 handover:** the full, code-checked guide is [`handover/FRONTEND-GUIDE.md`](handover/FRONTEND-GUIDE.md), with the API reference, flows and diagrams beside it. Where this page differs, `handover/` wins.

The backend serves one versioned REST API under **`/api/v1`**. Its contract is `openapi.json` in `packages/schemas`, released as **`schemas-v1.0.0`**.

## 1. Install the contract

```json
"@mkos/schemas": "npm:@maniacultasset/schemas@1.0.0"
```

- **The package.** It is on GitHub Packages, in the `maniacultasset` org. Your `.npmrc` needs a token with `read:packages`.
- **What's in it:** the generated TypeScript types for every request and response, and the OpenAPI document.
- **Never hand-write a type the package already has.**
- **Version rule.** Later additive changes ship as `1.x` MINORs, and the CHANGELOG lists each one. Breaking changes wait for the MAJOR window.

## 2. Work before the backend is deployed

- **The mock server.** `make mock` serves every operation from the OpenAPI examples on `127.0.0.1:4010`. Send `Prefer: code=<status>` to get a declared error. `make mock-smoke` checks that it answers.
- **The real API on your machine.** Run `make dev`, then `make seed`, in the backend repo; it serves on `127.0.0.1:8000`. The demo seed creates two tenants (`acme` and `bharat`), their users and 8 products.

## 3. Conventions every screen relies on

| Topic | Rule |
|---|---|
| Auth | `POST /auth/login` returns an access token and a refresh token. Send `Authorization: Bearer <access>`, and renew with `POST /auth/refresh`. `GET /auth/me` returns your memberships, and `POST /auth/switch-tenant` changes tenant. The tenant comes only from the token: no route takes a `tenant_id`. |
| Roles | `owner`, `admin`, `operator` and `reviewer`. A forbidden action is 403, and another tenant's object is 404, never 403. |
| Errors | Every error has one envelope: `{"error": {"type", "message", "status", "correlation_id", "details", "error_report"}}`. `error_report` is the shared `ErrorReport`. Show `message`, and log `correlation_id`. |
| Idempotency | These operations take an `Idempotency-Key` header: `products_bulk`, `imports_rows_retry`, `brand_documents_upload`, `images_upload`, `listings_generate`, `listings_regenerate`, `listings_approve`, `listings_publish`, `listings_retry_fields` and `jobs_cancel`. A retry with the same key and body replays the first answer. The same key with a different body is 409 `IDEMPOTENCY_KEY_REUSED`. |
| Async work | Long work returns **202** with a `job_id`. Poll `GET /jobs/{job_id}` until it reaches a terminal status, and use `POST /jobs/{job_id}/cancel` to stop it. |
| Pagination | Every list answers `{"data": [...], "page": {"next_cursor", "has_more", "limit"}}`. Pass `limit` (default 50, max 200) and `cursor`. There is no offset or page number. |
| Concurrency | `PATCH /tenants/me` uses `ETag` / `If-Match`. A stale write is 409, so re-read and retry. |
| Rate limits | Per tenant and per IP. A 429 carries `Retry-After`. |
| Request ids | Send `X-Request-ID` if you have one, and the API echoes it. Otherwise it generates one. |
| CORS | Only the `ALLOWED_ORIGINS` origins get a grant, with no credentials. Every method the API serves (`GET`, `POST`, `PATCH`, `DELETE`) is preflight-allowed. `ETag`, `Retry-After`, `Location` and `X-Request-ID` are exposed, so cross-origin JS can read them. |

## 4. The v1 screens and their operations

| Screen | Operations |
|---|---|
| Sign in, team | `auth_*`, `members_list`, `members_patch` |
| Brand profile and documents | `tenants_me`, `tenants_me_update`, `brand_documents_upload`, `brand_documents_list`, `brand_documents_delete` |
| Products and CSV import | `products_create`, `products_get`, `products_list`, `products_bulk`, and the import status, report and row-retry ops |
| Images | `images_upload_url` (signed PUT), `images_upload` (multipart), `images_report` |
| Marketplace accounts | `marketplaces_connect`: redirect the browser to the returned consent URL. Then `marketplaces_connect_confirm` after the callback lands, `marketplaces_accounts` and `marketplaces_accounts_revoke` |
| Listings: generate, review, edit, approve | `listings_generate`, `listings_regenerate`, `listings_get`, `listings_list`, `listings_content_patch`, `listings_validate`, `listings_approve`, `listings_status` |
| Publish | `listings_publish`, `listings_retry_fields` |
| Analytics | `analytics_get`, `analytics_summary` |
| Rules (support) | `rules_list`, `rules_dry_run` |

Until a backend step ships, its operations answer **501** on the real API, but they are fully specified in the contract and the mock.

## 5. The marketplace connect flow, step by step

1. The owner clicks "Connect Amazon" or "Connect Flipkart". Call `marketplaces_connect`, then send the browser to the consent URL it returns.
2. The marketplace sends the browser back to the backend's callback. The backend validates the state, exchanges the code, seals the tokens, and redirects to the dashboard with a one-time handle.
3. The dashboard calls `marketplaces_connect_confirm` with that handle. The account appears in `marketplaces_accounts`. Its status is `active`, or later `needs_reconnect`, which means you should show a "Reconnect" button.

## 6. MFA, lockout and passwords (contract 2.0.0; DPP-1, ADR-0021)

The backend ships with `MFA_ENFORCEMENT=off`: the login answers `status: "signed_in"` with today's
token fields, the MFA routes answer 501 "MFA is not enabled", and only the lockout and the
password rules are live. Build every screen below before the backend switches to `all`.

1. **Login second step.** Read `status` before storing tokens; only `signed_in` carries them. Any
   other status carries a `challenge_token`: keep it in memory only, never in localStorage. The code
   input uses `inputMode="numeric"` and `autocomplete="one-time-code"`, with a "Use a recovery code"
   link (`recovery_code`). POST `/api/v1/auth/login/mfa`. Add exactly the four challenge routes
   `/auth/login/mfa`, `/auth/login/mfa/enroll`, `/auth/login/mfa/confirm` and
   `/auth/password/expired` to the no-refresh public paths with **exact** matching, so a 401 there
   never triggers a refresh or a logout. The bearer routes `/auth/mfa/totp/enroll` and
   `/auth/mfa/totp/confirm` must **not** be on that list (they need the bearer).
2. **Enrolment.** At login (`mfa_enrollment_required`): POST `/auth/login/mfa/enroll
   {challenge_token}`, then `/auth/login/mfa/confirm {challenge_token, code}`; the answer carries
   `recovery_codes` and `result` (a `LoginResult`). In settings: POST `/auth/mfa/totp/enroll` (with
   `current_password` while there is no factor), then `/auth/mfa/totp/confirm {code}`; the answer
   carries `recovery_codes` only. Render `otpauth_uri` as a QR code on the client as a `data:` URL
   and show `secret_b32` grouped for manual entry. The `enroll` challenge lives 15 minutes.
3. **Recovery codes screen.** Show the 10 codes once, with copy and download, and an "I saved these"
   checkbox that gates Continue. `result` (when present) is the `LoginResult` to continue with. Warn
   when `GET /auth/security` shows `recovery_codes_left <= 3`.
4. **Account security settings.** `GET /auth/security` gives the MFA status, codes left,
   enforcement, grace date and password expiry date. Replace the authenticator with the
   `/auth/mfa/totp/*` pair; when a factor exists it needs a step-up done with a TOTP **or a recovery
   code** (lost phone). Regenerate codes with `POST /auth/mfa/recovery-codes` under a step-up.
   Change the password with `POST /auth/password`. Show a grace banner while `mfa_grace_until` is
   set and no factor exists.
5. **Password change and expiry.** On `password_change_required`, show the new-password screen and
   POST `/auth/password/expired` with the challenge. Forms show the 422 rule messages from
   `error.message` and `error.details`. Rules: 12+ characters, upper, lower, digit and symbol, no
   name or email part, not one of the last 10; a change is allowed once a day; a password expires
   after 365 days.
6. **Step-up dialog.** On 403 `MFA_STEP_UP_REQUIRED`, ask for a code (or a recovery code, except on
   admin actions: account revoke and the sign-up approve/reject take a TOTP only), POST
   `/auth/mfa/step-up`, swap the access token, and retry the request once. `StepUpOut` has no
   `refresh_token`: swap only the access token and never pass it through the code that stores both
   tokens (it would wipe the stored refresh token). A 401 from step-up means the session has no MFA
   family yet: refresh once, then retry. On 403 `MFA_ENROLLMENT_REQUIRED`, route to enrolment.
7. **Under `off`** the MFA routes answer 501; hide the MFA card when `GET /auth/security` shows
   `enforcement: "off"`.
8. **Signup form (live now).** `POST /signup` answers 422 `VALIDATION_FAILED` for a password that
   breaks the rules (the name rule checks the email local part and `owner_name`); show the
   `details` messages under the password field and list the rules as helper text. The `request_id`
   in the 202 is not stable for an existing email: do not store or compare it; read the request
   through `POST /signup/status`.
9. **Deploy order.** The backend release (still `off`) goes out before the FE change. If the FE
   ships first, treat a login answer with no `status` as `signed_in`. `/auth/switch-tenant` and
   `/auth/refresh` can answer 401 under `all` for a session whose code is 7 days old: treat it as an
   expired session.
10. **Lockout.** 10 failed passwords or codes lock the account for 15 minutes; the login keeps
    answering the same 401 (there is no "locked" message). Staff unlock it on request.

## 7. Sign in and sign up with Google (contract 2.1.0; ADR-0020)

The backend ships with `GOOGLE_OAUTH_ENABLED=false`: `GET /api/v1/auth/google/config` answers
`{"enabled": false}`, `start` redirects to `/login?error=google_unavailable` and the JSON steps
answer 501. Show the button only while `config.enabled` is true, and keep the notice for
`error=google_unavailable` (the flag can go off while a sign-in is under way). Google only proves who the person is:
under MFA the usual code (or enrolment) step always follows.

1. **The button** ("Continue with Google" on `/login`, "Sign up with Google" on `/signup`, the
   official multicolour G, above an "or" divider). On click: make a 32-byte random secret
   (`crypto.getRandomValues`, base64url, 43 characters), keep `{bind: secret, return: <safe return
   path>, intent: "login" | "signup"}` in `sessionStorage["gsi"]`, then
   `location.assign("/api/v1/auth/google/start?b=" + base64url(sha256(ascii(secret))))` (no padding).
   Hash the 43-character text, never the 32 raw bytes. Test vector: the secret of 32 zero bytes is
   `AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` and its `b` is
   `DwBzhbb51LfusnSGBa_hqYSgo7-j8BTQnip4TOnlzRo`.
   Always a same-origin relative path through the `/api` proxy, as a full page navigation (the
   backend refuses a `start` whose `Sec-Fetch-Site` is not `same-origin` or whose `Sec-Fetch-Mode`
   is not `navigate`, with a redirect to `/auth/google?error=failed`). The intent only picks the copy.
2. **The app route `/auth/google`** receives `?handle=<opaque>` or
   `?error=cancelled|failed|unavailable` (`unavailable`: the "not available" notice, as for
   `/login?error=google_unavailable`). Read it, strip it with `history.replaceState`, read and
   remove `sessionStorage["gsi"]` (no secret: show the generic failure with a link to `/login`), and
   POST `/api/v1/auth/google/complete {handle, binding: secret}`. Every answer carries `email`
   (Google's verified email) to show on the steps that follow. Act on `outcome`:
   - `sign_in`: `login` is a `LoginResult`: `signed_in` stores the tokens and goes to the saved
     return path (re-apply the safe-redirect check); any other status continues with
     `login.challenge_token` at the usual code, enrolment or password-change step.
   - `pending`: `signup` is the request status card (pending, rejected with its reason, approved).
   - `needs_details`: "No Kuinn account for `<details.email>` yet": the sign-up form, prefilled
     (`owner_name`, else `given_name` + `family_name`, editable; `suggested_brand_name` when
     present, editable, computed by the backend so no domain list is kept in the app; the email read-only with
     "Verified by Google"; no password field). POST `/api/v1/signup/google {details_handle, binding,
     brand_name, owner_name, phone?, website?}` -> 202 `SignupStatusOut` (pending). The handle lives
     15 minutes; keep the binding secret in memory for it (it was removed from sessionStorage).
   - `link_required`: "A Kuinn account already uses `<link.email>`": ask its password once, POST
     `/api/v1/auth/google/link {link_handle, binding, password}` -> a `LoginResult`, then the usual
     code steps. A wrong password is the login's 401 and the handle stays usable for 15 minutes.
   - Errors: 401 `OAUTH_STATE_INVALID` ("This Google sign-in has expired. Start again."), 401
     `UNAUTHENTICATED` (one sentence for every refusal: "Couldn't sign you in with Google. If you
     already have an account, sign in with your email and password."), 429, 501.
3. **Public paths.** Add exactly `/auth/google/complete`, `/auth/google/link` and `/signup/google`
   to the no-refresh public paths (exact match). Keep handles, the binding secret and the challenge
   token in memory only; never put them in logs, analytics or a URL of your own.
4. **Settings > Security.** `SecurityOut.has_password === false` (an account made with Google):
   hide "Change password" and the password dates. `google_linked`: show "Google sign-in: on" with a
   "Remove" button: POST `/api/v1/auth/google/unlink` (204) after a step-up (403
   `MFA_STEP_UP_REQUIRED` as for other sensitive actions); a 422 means the account has no password.
5. **Admin queue.** `SignupRequestOut.verified_via === "google"`: show a "Verified by Google" badge.
6. **One origin.** The binding lives in one tab's sessionStorage: the sign-in must finish in the tab
   that started it, on the dashboard origin the backend's `DASHBOARD_BASE_URL` names.

