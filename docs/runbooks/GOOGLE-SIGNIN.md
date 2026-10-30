# Sign in with Google: switch it on, verify, switch it off (ADR-0020)

Google sign-in ships switched off (`GOOGLE_OAUTH_ENABLED=false` in `scripts/deploy/gcp/prod.env`).
Turning it on is three things: a Google Cloud console client (by hand, once), its secret in
Secret Manager (typed in your own terminal), and a same-image redeploy with the switch on. Turning
it off is the same redeploy with the switch off. The migration (0012) is additive and stays.

Google only proves who the person is. Every Google sign-in still asks for the Kuinn login code
(or the authenticator setup on the first sign-in), and a new brand still waits for a platform
admin's approval.

## 1. Google Cloud console (once, by hand; about 15 minutes)

Project `aimos-prod` -> **Google Auth Platform**:

1. **Branding:** app name "Kuinn"; support and developer email; home page and privacy policy on
   the dashboard's domain. No logo (a logo triggers brand verification).
2. **Audience:** External, then **Publish app**.
3. **Data access:** only `openid`, `email` and `profile` (non-sensitive: no review).
4. **Clients -> Create -> Web application** (`aimos-prod-web`):
   - **Authorized redirect URI**, exactly one: `$DASHBOARD_BASE_URL/api/v1/auth/google/callback`
     (today `https://kuinn-web.vercel.app/api/v1/auth/google/callback`). The API derives the same
     string from `DASHBOARD_BASE_URL`, so they cannot drift.
   - **No** Authorized JavaScript origins (the flow is server side).
5. Put the **client id** (public) in `prod.env` as `GOOGLE_OAUTH_CLIENT_ID` (already there for the
   `aimos-prod-web` client). Never paste the **client secret** anywhere but step 2.

For local testing with a real Google account, create a separate client in `aimos-staging` with the
redirect `http://localhost:3000/api/v1/auth/google/callback`; keep localhost off the prod client.
Google deletes a client after 6 months unused (with a 30-day warning).

## 2. The client secret (your own terminal)

1. Set `GOOGLE_OAUTH_ENABLED=true` in `prod.env` (do not commit yet).
2. `scripts/deploy/gcp/deploy.sh prod setup --dry-run`, then `deploy.sh prod setup`. With the
   switch on, `setup` creates the `GOOGLE_OAUTH_CLIENT_SECRET` secret, grants only `aimos-api`
   access, and, while it has no value, prints the one command to add it:
   `bash -c 'read -rsp "GOOGLE_OAUTH_CLIENT_SECRET: " v; ...'`. Run that command yourself and paste
   the secret at the hidden prompt. It never reaches the screen, argv or shell history.
3. Rerun `deploy.sh prod setup`: it reports `secret GOOGLE_OAUTH_CLIENT_SECRET: present`.

## 3. Switch it on (same image)

1. Commit `GOOGLE_OAUTH_ENABLED=true` through a PR (the env file is the only switch: a value set by
   hand on Cloud Run is wiped by the next deploy, because `--env-vars-file` replaces every
   variable).
2. `deploy.sh prod deploy --sha <the live sha>` (a same-image redeploy; `google guard` refuses to
   deploy when the secret has no value). The API refuses to boot without the client id or with a
   weak secret, and refuses any override of Google's endpoints.
3. Verify:
   - `curl -s -o /dev/null -w '%{redirect_url}' "$API/api/v1/auth/google/start?b=$(printf %043d 0)"`
     shows `accounts.google.com/o/oauth2/v2/auth` with your client id and the exact redirect URI.
   - A real Gmail account on the dashboard: "Continue with Google" -> the prefilled sign-up ->
     approve it in the admin queue (it shows "Verified by Google") -> "Continue with Google" again
     -> the authenticator setup -> signed in.
   - An existing account's Google: `link_required` -> its password -> its code -> signed in; the
     next Google sign-in asks only for the code.
   - Logs and Sentry hold no email, code, handle or token for these requests (only reason codes
     and ids).

## 4. Switch it off (rollback)

Set `GOOGLE_OAUTH_ENABLED=false`, commit, and redeploy the same image. `start` then redirects to
`/login?error=google_unavailable` and the JSON steps answer 501; accounts created with Google keep
their data and sign in again when the switch comes back. Linked password accounts keep using their
password. The secret is no longer bound to the API.

## 5. Moving to another dashboard origin (e.g. kuinn.in)

The browser binding lives in one origin's sessionStorage and the callback goes to
`DASHBOARD_BASE_URL`, so Google sign-in works on that one origin. To move:

1. Add the new redirect URI (`https://kuinn.in/api/v1/auth/google/callback`) to the console client.
2. Set `DASHBOARD_BASE_URL=https://kuinn.in` and add it to `ALLOWED_ORIGINS` in `prod.env`;
   redeploy.
3. Redirect the old dashboard origin to the new one (a Vercel domain setting); remove the old
   redirect URI and origin later.

## 6. What is audited and logged

| Event | Audit action (reason) |
|---|---|
| A refused Google identity at the callback | `auth.login_failed` (`google_not_authoritative`, `google_token_invalid`, `google_exchange_failed`, `state_invalid`) |
| A refused `complete` | `auth.login_failed` (`google_admin`, `inactive_user`, `google_link_refused`, `google_unvetted_conflict`, `no_active_membership`, `locked`, `busy`) |
| A Google sign-in that ends with tokens | `auth.login` (`google`) |
| A Google account linked after the password and code | `auth.google_linked` |
| A link removed in settings | `auth.google_unlinked` |
| A Google sign-up | `signup.requested` (`google`) |

The daily maintenance job deletes expired `login_flows` rows and warns (log and Sentry,
`signup.pending_backlog`) when more than 200 sign-up requests wait for approval.

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| Google shows `redirect_uri_mismatch` | The console's redirect URI is not exactly `$DASHBOARD_BASE_URL/api/v1/auth/google/callback`. |
| `/auth/google?error=failed` for every account | The client secret is wrong (the token exchange is refused), or the account is not one Google owns (an external email registered at Google). See the `auth.login_failed` reason. |
| `/auth/google?error=failed` after a long pause on Google's page | The sign-in took more than 10 minutes; start again. |
| The app says the sign-in expired | The 60-second handle was used, expired, or completed in another browser tab (the binding lives in one tab's sessionStorage). |
| `deploy.sh` stops with `GOOGLE_OAUTH_ENABLED=true needs GOOGLE_OAUTH_CLIENT_ID` | The env file's client id is blank or not a `*.apps.googleusercontent.com` id. |
