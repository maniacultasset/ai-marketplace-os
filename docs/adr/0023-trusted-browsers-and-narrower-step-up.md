# ADR-0023: Trusted browsers and a narrower step-up

- **Status:** proposed
- **Date:** 2026-10-30
- **Deciders:** the user (Tech Lead); product decisions approved in chat on 2026-10-30,
  acceptance pending
- **Amends:** ADR-0021 §5 (step-up) and §6 (7-day re-authentication); ADR-0020 §5 (Google is
  never a second factor: unchanged, but a trusted browser may now stand in for the Kuinn code)

## Context

Under `MFA_ENFORCEMENT=all` (ADR-0021) every sign-in asks for a TOTP code, and seven operations
ask for a fresh one (step-up) within 10 minutes, including the platform admin's sign-up approve
and reject. Users asked for a smoother sign-in on their own computer without weakening what the
DPP asks: MFA for all user accounts (DPP 1.4.1), lockout after 10 failures (1.2.2) and the
password rules.

## Decision

1. **Trusted browser.** After a correct TOTP code (never a recovery code) at a sign-in code step
   (password path and Google path, and the forced enrolment), the user may choose "Don't ask for a
   code on this browser" (checked by default in the app). The server creates one
   `trusted_devices` row (migration `0013_trusted_devices`) and returns a random token: 256 bits,
   43 base64url characters. Only `sha256("mkos-trusted-device|" || token)` is stored, bound to the
   user. The trust lasts a fixed 30 days from that code, 7 days for a platform admin (a CHECK caps
   30, a guard trigger caps admins at 7). At most 10 per user; the oldest is evicted.
2. **Use.** The browser sends the token in the JSON body only (`device_token` on `/auth/login`,
   `device_tokens` with up to 5 on `/auth/google/complete`). It is read only after the password
   (or Google) step passed, under the per-user row lock, and matched under that user only, in
   constant time. It stands in for the code only when the code is the last thing the sign-in
   misses: never for enrolment, a forced password change, or a Google link. Each use swaps the
   token for a new value (the old hash is kept as `prev_token_hash`); the expiry never moves. A
   rotated token presented again is treated as theft: that browser is forgotten, its sessions
   revoked, and the sign-in asks for the code.
3. **Tokens.** A trusted sign-in mints no new `amr` value: the first factor proven now (`pwd`, or
   nothing for Google) plus `otp`, with `mfa_at` = the time the code that created the trust was
   entered. `AuthMethod`, `JWTClaims` and the refresh `amr` CHECK are unchanged.
4. **Re-authentication (amends ADR-0021 §6).** A refresh family begun by a trusted browser
   records it (`refresh_tokens.device_id`, `device_auth_at`). While no code was entered since, the
   7-day rule counts from that sign-in and holds only while the browser is still trusted; a
   step-up moves `mfa_at` past `device_auth_at` and the ordinary rule applies again. The tenant
   switch copies both fields. When the user keeps the current session while dropping its browser
   trust (sign out all, a password change from settings), `device_id` is cleared and
   `device_auth_at` kept, so that session still ends 7 days after its trusted sign-in.
5. **Step-up (amends ADR-0021 §5).** The window is 30 minutes (1800 s). The step-up stays on the
   high-risk operations only: marketplace connect and revoke, member changes (removal, role and
   owner), authenticator replace, recovery-code regeneration and Google unlink. It is removed from
   the platform admin's sign-up approve and reject, which stay platform-admin only. A trusted
   sign-in passes a step-up only in the 30 minutes after the real code that created the trust;
   after that a real code is asked.
6. **Revocation.** One browser (`DELETE /auth/devices/{id}`, which also ends its sessions), every
   browser (`POST /auth/devices/sign-out-all`, which ends every other session and keeps the
   caller's), any password change (self-service, forced, staff `set-password`), authenticator
   enrolment or replacement, staff `reset-mfa` and `make-admin`, account erasure, eviction past
   10, expiry (refused at once, deleted by the daily retention run) and token reuse. Audit rows:
   `auth.device_trusted` and `auth.device_revoked` (with the reason); log line `auth.device_reuse`.
7. **Contract 2.1.0 (unreleased), additive.** New optional request fields `device_token`,
   `device_tokens`, `trust_device`; new optional `LoginResult` fields `device_token`,
   `device_expires_at`, `device_token_invalid`, `device_trust_days`; three bearer operations
   (`auth_devices_list`, `auth_devices_remove`, `auth_devices_sign_out_all`; 72 -> 75). No new
   error code. The two admin sign-up operations no longer declare the step-up 403 codes.

## Consequences

- Every account still enrols (MFA_ENFORCEMENT=all, grace to 12 Nov), the lockout at 10, the
  password rules and the "no Google sign-in for admins" rule are unchanged. A trust is created
  only by a correct TOTP code, so "MFA for all user accounts" still holds.
- Known limits, accepted:
  - **Password plus a stolen device token skips the code** for up to 30 days (7 for admins).
    Bounded by the fixed expiry, rotation with reuse revocation, per-user binding, Remove, sign out
    all, and every credential change. XSS can read the token from localStorage, as it already can
    the refresh token (ADR-0021); a device token alone still needs the password or the Google
    account.
  - **The 30-minute step-up window** is weaker than 10 minutes for a stolen access token right
    after a code.
  - **Sign-up approval without a step-up**: an admin access token (30 minutes; admin sessions need
    a code every 7 days, no Google for admins) can approve or reject; approval creates a trial
    tenant only, and the audit row stays.
  - **No email notice** of a new trusted browser (ADR-0021's limit stands); the list in Settings >
    Security is the place to check.
  - **No global kill switch**: an incident that needs every trust gone deletes the rows through
    the staff (migrator) path.
- Rollout: the backend first, because an older API refuses unknown request fields (strict
  models); the frontend sends the new fields only after this API is live.

## Alternatives rejected

- **A sliding expiry**: a browser used weekly would stay trusted for ever; a fixed expiry forces a
  code at least every 30 days.
- **A new `amr` value (`dev`)**: it would widen a closed vocabulary (a MAJOR contract change) and
  let a trusted sign-in look fresh to the step-up; reusing `otp` with the original code time keeps
  the step-up honest.
- **An HTTP-only cookie**: the API is cross-origin and token based (ADR-0006); a cookie would add
  CSRF handling for one feature.
