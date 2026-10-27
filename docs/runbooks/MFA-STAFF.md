# MFA, lockout and staff recovery (DPP-1)

How staff unlock an account, reset a lost authenticator, and switch MFA on, for the MFA, lockout
and password policy of ADR-0021 (Amazon DPP 1.2.2 and 1.4.1). The design is in
`docs/architecture/BACKEND-ARCHITECTURE.md` §9.2; the settings are the `MFA_*` rows of
`docs/canon/ENV.md`.

## 1. Before any unlock or reset: the identity check

Never unlock or reset on the strength of an email or a chat message alone: that is how an account
is taken over.

1. Take the request only from the brand's known contact channel (the owner's phone number on file,
   or the email thread the brand used at onboarding), never from the address that asks.
2. Call the person back on that known number and confirm their name, brand and the email of the
   account.
3. For an owner, confirm with a second owner of the brand when one exists.
4. Record who asked, who verified, how and when, in the support ticket, before running anything.

The commands write their own audit rows (`auth.unlocked`, `auth.mfa_reset`, actor `staff` with your
email), which an alert or a review can match to the ticket.

## 2. The commands

### Production and staging (Cloud Run jobs)

```bash
# a locked account (10 failed passwords or codes; it also unlocks itself after 15 minutes)
scripts/deploy/gcp/deploy.sh prod tenant unlock --email user@brand.example --staff-email you@kuinn.in

# a lost authenticator: deletes the factor and the recovery codes, sets a new must-change
# password (generated into Secret Manager), ends every session
scripts/deploy/gcp/deploy.sh prod tenant reset-mfa --email user@brand.example --staff-email you@kuinn.in
```

`reset-mfa` prints the `gcloud secrets versions access` command that shows the new password once.
Hand it to the person over the verified channel. At the next sign-in they enrol a new authenticator
and choose their own password. `unlock` mounts no password and generates none.

### Local (or any shell with `ALEMBIC_DATABASE_URL`)

```bash
uv run --frozen python -m app.cli user unlock --email user@brand.example --staff-email you@kuinn.in
uv run --frozen python -m app.cli user reset-mfa --email user@brand.example --staff-email you@kuinn.in
# reset-mfa asks for the new password twice on a terminal, or reads one line of stdin
```

Exit codes: 0 done; 2 bad arguments, or a password that breaks the rules (the message names the
rule, never the password); 4 unknown user; 1 any other failure.

## 3. Switching MFA on (the rollout order)

`MFA_ENFORCEMENT` and `MFA_GRACE_UNTIL` live only in `scripts/deploy/gcp/<env>.env`. A value set by
hand on Cloud Run is wiped by the next deploy (`--env-vars-file` replaces every env var). There is
no "enrol first under `off`" step: the MFA routes answer 501 under `off`.

1. **Release with `off`.** Merge, then `deploy.sh prod release`. The lockout and the set-time
   password rules go live at once; logins and tokens are otherwise unchanged.
2. **Create the MFA key.** `deploy.sh prod setup` creates `mfa-secret-key` and binds only the api
   service account to it (encrypt and decrypt); then `deploy.sh prod release` again.
3. **Release the web app** that handles every sign-in `status`, the enrolment, the recovery codes,
   the step-up dialog and the password screens (`docs/HANDOFF-FE.md` §6).
4. **Staging under `all`.** `deploy.sh staging setup` (creates `mfa-secret-key` and its grant in
   `aimos-staging`), then set `MFA_ENFORCEMENT=all` and `MFA_GRACE_UNTIL` in `staging.env` and run
   `deploy.sh staging release`. Enrol and test the whole flow on staging under `all`.
5. **Production under `all`.** Only after the checklist below: edit `prod.env`
   (`MFA_ENFORCEMENT=all`, `MFA_GRACE_UNTIL` 14 days ahead, ISO 8601 with an offset, e.g.
   `2026-11-20T00:00:00+05:30`) and run `deploy.sh prod release`. `deploy.sh` refuses `all` without
   a grace date. Platform admins and first logins must enrol at once; everyone else by the grace
   date.
6. **Rollback.** `MFA_ENFORCEMENT=off` in `prod.env` and `deploy.sh prod release`. Factors and codes
   stay stored and work again when `all` returns.

### Checklist before `all` in production

- [ ] The shared prod test login is retired: a shared account cannot carry one person's
      authenticator (DPP 1.2.1 also forbids shared accounts).
- [ ] The web app release of step 3 is live, and staging passed step 4.
- [ ] `deploy.sh prod setup` has run since this release (the key and its grant exist).
- [ ] Platform admins know they must enrol at their next sign-in.
- [ ] Support knows this runbook (the identity check of §1).

### What the DPP questionnaire may say, and when

| Stage | MFA (1.4.1) | Lockout (1.2.2) | Password rules (1.4.1) |
|---|---|---|---|
| This release deployed, `off` | "No" (built, not enforced) | "Yes" | "Yes" for the set-time rules; expiry and forced change "not yet" |
| `all` switched on, grace running | "Yes, enforced; existing users have until the grace date" | Yes | Yes |
| Grace over, plus 2-step verification on GCP, GitHub, Vercel and the Seller Central account, the first quarterly access review, and the shared prod test login retired | "Yes, all user accounts" | Yes | Yes |

## 4. Local testing with `all`

Set `MFA_ENFORCEMENT=all` (and optionally `MFA_GRACE_UNTIL`) in your `.env`; no KMS key is needed
locally (the LocalKmsStub seals with its own MFA label). Every seed user is a first login with no
factor, so the login answers `mfa_enrollment_required`: enrol with an authenticator app (or compute
codes from `secret_b32`). The seed password `devpassword123` breaks the DPP rules, so after the code
step the login answers `password_change_required`: set a compliant password through
`/auth/password/expired`.

## 5. What to watch

- `auth.locked` log lines (and audit rows): many for one account means a guessing or lockout
  attack; many across accounts from one `ip_hash` means spraying.
- `auth.mfa_failed`, `auth.mfa_recovery_used` and `auth.mfa_reset`: a recovery code used without a
  support ticket is worth a call to the user.
- `auth.refresh_reauth_required`: sessions ended by the 7-day rule (expected, weekly per user).
