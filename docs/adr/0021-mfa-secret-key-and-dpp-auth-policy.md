# ADR-0021: MFA secrets under their own KMS key; password and lockout policy for Amazon DPP

- **Status:** proposed
- **Date:** 2026-10-27
- **Deciders:** the user (Tech Lead); content pre-approved in chat on 2026-10-27, acceptance pending

## Context

Amazon's Data Protection Policy (DPP) is a precondition for SP-API developer access. The clauses
this decision answers, as quoted in the workspace proposal of 2026-10-26 from
https://sellercentral.amazon.com/mws/static/policy?documentType=DPP&locale=en_US :

- 1.4.1 "Solution Provider must require Multi-Factor Authentication (MFA) for all user accounts".
- 1.2.2 "account lockout controls after no more than ten (10) consecutive unsuccessful login
  attempts".
- 1.4.1 password rules: minimum 12 characters, a composition mix, no part of the name, minimum age
  1 day, maximum age 365 days, a history of 10. [unverified: this is the proposal's summary, not
  the clause text; quote the clause verbatim here before this ADR is accepted.]

Before this change the API had a password-only login (argon2id), per-minute login buckets and no
lockout, no composition, age or history rule, and one KMS key (`oauth-token-key`, CANON.md "KMS
variable name") that the API may only encrypt with (ADR-0018: decrypt only in the worker).

## Decision

1. **TOTP MFA** (RFC 6238: HMAC-SHA1, 6 digits, 30 s steps, one step of skew either way, 160-bit
   secrets), standard library only, each code usable once (`last_used_step`). Ten recovery codes
   of 128 bits each, stored as `sha256(user_id || code)` and shown once. The second sign-in step
   carries an opaque challenge token (only its sha256 is stored; 5 attempts; 5 minutes, or 15
   minutes to enrol). One shared `_sign_in()` decides between tokens and a challenge for a code,
   an enrolment or a password change.
2. **A second KMS key, `mfa-secret-key`**, on the `marketplace-os` ring, for MFA secrets only. The
   API service account gets `cryptoKeyEncrypterDecrypter` on that key alone (it must open a secret
   to check a code); the worker and the jobs get no binding, and the `oauth-token-key` bindings do
   not change. This amends the one-key line of CANON.md and ADR-0018's decrypt-only-in-the-worker
   rule for this key only. The secret is sealed with a generic `seal_bytes` (the token envelope's
   layout under its own magic `MFA1`, AAD `"mfa-totp|" + user_id` and data-key label
   `mkos-mfa-dek|`), and its unseal half lives in `app.adapters.mfa_secrets`, which only
   `app.services.mfa` may import (import contract `mfa-secret-unseal-only-in-mfa-service`).
   Locally the LocalKmsStub seals with the MFA label. `MFA_KMS_KEY_URI` is required for a deployed
   API under `MFA_ENFORCEMENT=all`, must differ from `KMS_KEY_URI`, and a worker refuses it.
3. **Enforcement switch** `MFA_ENFORCEMENT=off|all` with `MFA_GRACE_UNTIL`. Under `off` the login
   and tokens behave as before MFA (the MFA routes answer 501); the lockout and the set-time
   password rules are live in both modes. Under `all` platform admins and first logins enrol at
   once, others after the grace date. `deploy.sh` refuses `all` without an offset grace date.
4. **Claims** `amr`, `mfa_at` and `sid` (the refresh family) are optional (contract 2.0.0) and
   only minted under `all`, so a token minted under `off` keeps the ten frozen claims and an older
   API still accepts it. Refresh rows store the session proof and rotation copies it.
5. **Step-up** (amended by ADR-0023: 1800 s, and no longer on the sign-up approve and reject):
   sensitive routes need a code entered in the last 600 s under `all` (member
   changes and the marketplace connect take a TOTP or a recovery code; the account revoke and the
   sign-up approval take a TOTP code only). A user who lost the phone signs in with a recovery
   code, steps up with another, and binds a new authenticator without staff.
6. **7-day re-authentication** under `all` past grace (none for admins): a refresh, or a tenant
   switch, is refused when the session's code is missing or 7 days old. (Amended by ADR-0023: a
   session begun by a trusted browser counts its 7 days from that sign-in, and only while the
   browser stays trusted.)
7. **Lockout**: one persisted counter for wrong passwords and wrong codes on every verify path
   (login, the code steps, both enrolments, step-up, both password changes, sign-up and its
   status). At 10 the account locks for 15 minutes behind the same 401 as an unknown email, after
   the same single argon2 verify on the public password routes. The counter is atomic SQL
   (`app_note_auth_failure`) and clears only after a full sign-in or a staff unlock. Each verify
   path first takes the user row `FOR UPDATE` (`app_begin_auth_attempt`, 5 s lock timeout), so
   parallel attempts cannot all get a real check before the 10th failure lands. Every counted
   failure commits before the 401/422 is raised.
8. **Passwords, DPP 1.4.1 read literally**: 12 to 1024 characters with an upper-case letter, a
   lower-case letter, a digit and a symbol; no email local part or name token of 3+ letters
   (NFKC, case folded); none of the last 10 (the current hash plus 9 kept); a self-service change
   at most once a day; past 365 days, or set by staff, or failing the current rules, the next
   sign-in under `all` forces a change. Staff-set passwords (`set-password`, `reset-mfa`, a new
   user) are must-change. This departs from NIST SP 800-63B-4 §3.1.1.2, which says verifiers
   "SHALL NOT impose other composition rules"; the DPP clause wins for this product.
9. **Staff CLI** `user unlock` and `user reset-mfa` (one audited migrator transaction each),
   reached in production through `deploy.sh tenant unlock|reset-mfa`, after the identity check of
   `docs/runbooks/MFA-STAFF.md`.

## Consequences

- MFA, lockout and the password rules meet the DPP clauses above; the WAF (DPP 1.1.1) is a
  separate decision, blocked on the domain.
- The API can decrypt MFA secrets. A compromised API process can read them; the separate key, the
  AAD bound to the user and the import contract keep that path narrow and away from marketplace
  tokens.
- Known limits, accepted:
  - **No access-token denylist**: a reset or logout leaves an issued access token live for up to
    30 minutes.
  - **Refresh token in localStorage** (frontend): XSS can steal it; the 7-day rule bounds the
    replay window.
  - **Lockout DoS**: whoever knows an email can keep that account locked with 10 guesses every
    15 minutes. Mitigations: the unlock CLI and the `auth.locked` log line for an alert.
  - **Slow TOTP guessing**: the counter resets at each lock, so a password holder gets about 10
    code guesses per 15 minutes indefinitely. This deviates from NIST SP 800-63B-4 §3.2.2 (at most
    100 consecutive failures); `auth.locked` is the detection signal.
  - **Trust on first use**: a user with no factor, in grace or at a first login, can be enrolled by
    whoever holds the password. Mitigations: the `auth.mfa_enrolled` audit row and log line, and
    `reset-mfa` after the identity check.
  - **The counter clears only after a full sign-in or a staff unlock**: a successful step-up or a
    verified current password does not clear it ("consecutive" is per sign-in).
  - **Pool pressure**: a request waiting on a user's row lock holds a database connection for up
    to the 5 s lock timeout (pool 5 + 5 overflow). The login routes are bounded by the login
    limiter; the challenge routes only by the per-IP limiter, and reaching the lock needs a live
    challenge token, which needs the password and a limited login.
  - **No email notice** of security changes; they are seen only in the app.
  - **Trusted browsers (ADR-0023)**: a password plus a stolen trusted-browser token skips the
    sign-in code for up to 30 days (7 for admins), bounded by the fixed expiry, rotation with
    reuse revocation and revocation on every credential change; a session kept while its
    browser's trust is dropped never outlives that trust, so the admin 7-day bound holds; the
    step-up window is 30 minutes and a trusted sign-in never counts as fresh for it.
- Contract 2.0.0 (two error codes, the login union) is a MAJOR release; the frontend must handle
  every sign-in `status` before the switch to `all`.
