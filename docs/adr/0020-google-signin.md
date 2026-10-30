# ADR-0020: Sign in and sign up with Google, always followed by Kuinn's own login code

- **Status:** proposed
- **Date:** 2026-10-30
- **Deciders:** the user (Tech Lead); the design and its defaults were approved in chat on
  2026-10-30, acceptance pending
- **Amends:** RULES §4 and ADR-0005 (`pyjwt[crypto]` "HS256 only") for Google ID tokens only;
  answers TEAM-QUESTIONS T97 (social login in v1).

## Context

Brand users want "Continue with Google" on the login and sign-up pages. Amazon's Data Protection
Policy 1.4.1 requires the solution's own multi-factor authentication for every account (ADR-0021),
so Google may prove who a person is but cannot replace Kuinn's login code. Self-serve sign-up
already exists behind platform-admin approval (ADR-0019). The API is reached through the
dashboard's `/api/*` proxy, sets no cookies and keeps tokens in memory.

## Decision

1. **Authorization-code flow with PKCE, run by the backend.** `GET /auth/google/start` stores a
   `login_flows` row (the PKCE verifier and the nonce's sha256, 10 minutes) and redirects to Google
   with S256, `state`, `nonce`, `prompt=select_account` and the scopes `openid email profile` only
   (no photo is asked for, stored or shown). The callback consumes the row once, exchanges the code
   over httpx (5 s connect, no redirects, never logged) and redirects to the app's `/auth/google`
   with an opaque handle (60 seconds). The server takes no redirect parameter: the app keeps its
   own return path. One start endpoint serves the login and the sign-up page; the page choice stays
   in the browser and changes no authorization.
2. **The browser binding.** Before leaving, the browser keeps a 32-byte random secret in
   sessionStorage and sends `start` only its sha256 (`b`). Every JSON step (`complete`, `link`,
   `/signup/google`) sends the secret, and a handle is consumed only with the right binding. This
   closes login CSRF and handle theft without a cookie.
3. **RS256 for Google ID tokens only**, in one module (`app/core/google_oidc.py`): RS256 alone
   (`none` and HS256 are refused before a key is read), a known `kid` from Google's JWKS (cached an
   hour, refetched at most once a minute on an unknown `kid`), `iss` Google's issuer (with or
   without the scheme, as Google documents), `aud` and `azp` the client id, `exp` and `iat` with a
   10 s leeway, `sub` required, the nonce equal to the one stored at `start`, and `email_verified`
   exactly true. Kuinn's own tokens stay HS256. No new dependency (`pyjwt[crypto]` and httpx).
4. **Google owns the email** only for `@gmail.com` (`@googlemail.com`) or a Workspace account
   whose `hd` is the email's own domain. Any other Google account (an external email registered at
   Google) is refused at the callback.
5. **Every Google sign-in goes through the shared `_sign_in()`** with `fed` as the only proven
   method. Google is a first factor, never a second one: under `MFA_ENFORCEMENT=all` the next step
   is always the Kuinn code, or the enrolment for a user with no factor, with no grace period. The
   per-user lockout gate runs first (a locked or busy account gets the generic failure, uncounted).
   `fed` is kept on the sign-in challenge only (`ck_mfa_challenges_amr` widened); a token's `amr`
   lists only Kuinn's factors, so the contract's `AuthMethod` is unchanged, and a step-up never
   claims a password a Google session did not prove. A user with no password never meets the
   password-change step.
6. **Outcomes of `complete`**, decided by one pure table: `sign_in` (the account linked to this
   Google account), `pending` (an applicant's own sign-up request, for a linked or same-email
   applicant), `needs_details` (no account: a prefilled sign-up, owner name from Google, a brand
   name suggested from a company email's domain), `link_required` (an account with this email and
   no Google account). Platform admins, inactive users, an email already linked to another Google
   account and an applicant without a request get one generic failure, audited with its reason.
7. **Linking needs the account's own password and code.** `link_required` hands out a link handle
   (15 minutes); the user enters the Kuinn password (`/auth/google/link`, counted toward the
   lockout), then the code or the enrolment, through the usual steps. The Google account waits on
   the challenge (`mfa_challenges.link_google_sub`, allowed only with `pwd` proven) and is linked
   only when that sign-in ends with tokens (`auth.google_linked`). No account is ever linked by
   email alone, for any domain. `POST /auth/google/unlink` (after a step-up) removes the link, and
   is refused while the account has no password.
8. **A new Google user** (`/signup/google`) is created with no password, `auth_provider =
   'google'`, `email_verified_at = now()` and Google's `sub`, plus a pending sign-up request
   marked `verified_via = 'google'`. A platform admin still approves every brand (ADR-0019).
9. **The database guards the identity.** `users.google_sub`, `auth_provider` and
   `email_verified_at` are written only by SECURITY DEFINER functions owned by migrator
   (`users_credential_guard`); `signup_requests.verified_via` is set once, by the definer. Erasure
   clears `google_sub` and deletes the user's flows. `login_flows` is a platform table under FORCE
   RLS reached only by the sha256 of its single-use value (a named invariant-5 exception).
10. **Switched off by default.** `GOOGLE_OAUTH_ENABLED=false`: `start` redirects to
    `/login?error=google_unavailable` and the JSON steps answer 501. The client secret lives in
    Secret Manager and is bound to the API only while the switch is on. Google's endpoints are
    constants outside `APP_ENV=local`. The redirect URI is derived from `DASHBOARD_BASE_URL`.
11. **No new error code.** A bad handle or binding is 401 `OAUTH_STATE_INVALID`; every refusal is
    401 `UNAUTHENTICATED` with one generic message (no account oracle). Contract 2.1.0 is MINOR:
    new operations and models and two optional fields.

## Consequences

- One person with two Google accounts could create a second, approved account; approval stays
  manual, and adding a member to a brand stays a staff procedure that confirms a different person
  out of band (T15 is unchanged).
- Workspace users whose domain lapses keep their password and code: linking still needs both.
- Google sign-in works on one dashboard origin at a time (the binding lives in that origin's
  sessionStorage and the callback goes to `DASHBOARD_BASE_URL`).
- Per-IP limits are only as good as the `X-Forwarded-For` hop count in front of the API.
- The consent screen shows the dashboard's domain until a custom domain and brand verification
  exist.

## Alternatives rejected

- Google Identity Services and One Tap: they need a CSP, COOP and `frame-src` change and load
  Google's script on our pages.
- Linking by email (even for Gmail): an email match alone would hand an account to whoever holds
  the Google identity; D1 asks for the password and the code once.
- Adding `fed` to `AuthMethod`: a closed-vocabulary change (MAJOR) for no gain; the audit row
  records Google instead.
