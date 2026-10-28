# Access management, staff accounts, devices and training

Amazon DPP 1.1.2, 1.2, 1.3, 1.4, 2.3 and AUP 3.9. Owner: the engineering lead, with the client
director as approver. Facts marked "checked" were read on 2026-10-28 with read-only commands.

## Principles

- One named account per person everywhere; no shared, generic or default logins (DPP 1.2.1).
- Access is denied by default and granted for a written business need, at the lowest role that
  does the job (DPP 1.3, AUP 3.9).
- Every account that can reach marketplace data uses MFA and a password manager (DPP 1.4.1).
- Only Approved Users (named, need documented, training done) get access (DPP 1.2.4).

## Request, approve, provision, revoke

| Step | Who | Record |
|---|---|---|
| Request | The person or their lead, with the reason and the systems needed | Access register below |
| Approve | Client director for GCP `aimos-prod` and Vercel; engineering lead for GitHub and the Kuinn platform-admin role | Access register |
| Provision | The account owner of that system, at the lowest role | Access register |
| Review | Quarterly (below) | Review record |
| Revoke | Within 24 hours of leaving or a role change (DPP 1.2.3), by the system owner | Access register |

Inside the product, brand owners manage their own members (roles owner, operator, reviewer,
admin; `apps/api/app/core/rbac.py`). Deactivating a member revokes their refresh tokens at once,
and their last access token expires within 30 minutes (`ACCESS_TOKEN_TTL_MINUTES`). Staff actions
on user accounts go through the audited staff commands only (`docs/runbooks/MFA-STAFF.md`).

## Systems and current state

| System | Who has access (checked) | Required control | State |
|---|---|---|---|
| GCP `aimos-prod` | One human: the company admin Google account, role Owner. No other human bindings | 2-Step Verification on the account; add a second named admin so the owner is not a single point of failure; consider moving from a personal Gmail to a company Google Workspace identity | 2SV not verifiable by API: **Owner to confirm** |
| GCP service accounts | `aimos-api`, `aimos-worker`, `aimos-migrate`, `aimos-ops`, `aimos-storage`, `aimos-build`, each with only its own roles (`deploy.sh setup_accounts`, `setup_kms`); no user-managed keys on any of them. Added by the next `setup`: `aimos-maint` (the daily maintenance job: Cloud SQL client and four secrets) and `aimos-sched` (may only run that job) | Documented owner: the engineering lead | Live (the two new ones after `setup`) |
| GCP default compute service account | Holds `roles/editor` on the project | Not used by any workload (builds run as `aimos-build`); remove the binding | **Owner action** (below) |
| Secret Manager | Per-secret `secretAccessor` for the runtime accounts only | A leftover `INITIAL_PASSWORD_*` secret from first-owner setup still exists | **Owner action**: delete it once that user has changed their password |
| GitHub org `maniacultasset` | 5 members, Free plan; branch protection is not available on Free private repos | "Require two-factor authentication" for the org; members' 2FA status is visible only to org owners | **Owner to turn on and confirm** |
| Vercel (dashboard hosting) | Not checked (no access) | Named members only, 2FA on each, Pro plan for commercial use | **Owner to confirm** |
| Amazon Solution Provider Portal | To be created | Named users only, each passes Amazon's identity check; MFA on the Amazon account | Client |
| Flipkart Partner Dashboard | To be created | Third-party application only; never a seller's self-access keys | Client |
| Kuinn production | Named users; one shared test login is still used for production smoke checks | Retire the shared login; give each tester a named account | **Owner action** |

Commands for the owner (run once; this repository never runs them):

```
gcloud projects remove-iam-policy-binding aimos-prod \
  --member=serviceAccount:1039991710726-compute@developer.gserviceaccount.com --role=roles/editor
gcloud secrets list --project=aimos-prod --filter='name~INITIAL_PASSWORD_'   # then delete each one no longer needed
```

## Programmatic credentials (DPP 1.4.2)

All live in Secret Manager (encrypted at rest, per-secret IAM), never in the repo (gitleaks on
every commit) and never in env files (`scripts/deploy/gcp/prod.env` header).

| Credential | Rotated | Next due |
|---|---|---|
| `JWT_SECRET`, `ADMIN_SESSION_SECRET` (rotating signs users out) | Yearly, or on suspicion | 2027-10-12 |
| Database role passwords (`APP_RW_PASSWORD`, `APP_RO_PASSWORD`, `MIGRATOR_PASSWORD`, `POSTGRES_PASSWORD`) and the URLs built from them | Yearly, or on suspicion | 2027-10-12 |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` (Cloud Storage HMAC) | Yearly, or on suspicion | 2027-10-12 |
| `OPENAI_API_KEY`, `SENTRY_DSN` | Yearly, or on suspicion | 2027-10-12 |
| Amazon LWA client secret, Flipkart application secret | Yearly, or on suspicion (follow any rotation date Amazon or Flipkart sets in its portal) | One year after creation |
| KMS keys `oauth-token-key`, `mfa-secret-key` | Automatically every 90 days | Automatic |

Rotation: add a new secret version, redeploy (`deploy.sh prod deploy`), confirm the smoke test,
then disable the old version. Record it here.

| Date | Credential | By | Note |
|---|---|---|---|
| | | | |

## Quarterly access review (DPP 1.2.2)

Every quarter (January, April, July, October) the engineering lead and the client director:
1. list GCP IAM (`gcloud projects get-iam-policy aimos-prod`), service account keys, Secret Manager
   accessors, GitHub org members and outside collaborators, Vercel members, the Amazon and
   Flipkart portal users, and Kuinn platform admins;
2. compare each with the access register; remove anything without a current need;
3. confirm MFA on every human account;
4. record the result below.

| Quarter | Reviewers | Removed | MFA confirmed | Notes |
|---|---|---|---|---|
| 2026 Q4 | | | | First review, due by 2026-12-31 |

## Access register

| Person (name, role) | System | Role granted | Approved by | Granted | Training done | Revoked |
|---|---|---|---|---|---|---|
| | | | | | | |

## Leavers and role changes (within 24 hours)

1. Disable the person's Kuinn accounts (deactivate memberships; `user reset-mfa` if they knew a
   shared device).
2. Remove them from GCP IAM, GitHub, Vercel, the Amazon and Flipkart portals and the team chat.
3. Rotate any secret they could read (table above) and the Amazon or Flipkart client secret if they
   had portal access.
4. Recover company devices; confirm no marketplace data stays on personal devices.
5. Record the time of each step in the access register.

## Devices (DPP 1.1.2, 2.3.1)

Every laptop or phone that can reach marketplace data or production must have:
- full-disk encryption on (FileVault, BitLocker), and the OS on automatic updates;
- the built-in anti-malware on with automatic updates (XProtect on macOS, Microsoft Defender on
  Windows), and users without the rights to turn it off;
- screen lock after at most 15 minutes, with a password;
- no marketplace data, exports or database dumps on removable media or personal cloud drives;
- device management (MDM) once the team issues company devices; until then, personal devices may
  reach only the dashboard and the cloud consoles (never data exports) and must meet the list above.

Device inventory, reviewed quarterly:

| Device | Owner | Encryption | Auto-update | Anti-malware | Screen lock | Last checked |
|---|---|---|---|---|---|---|
| | | | | | | |

Software inventory: the runtime stack is fixed by `uv.lock`, `apps/api/Dockerfile` (digest-pinned
base image) and `scripts/deploy/gcp/` (every cloud resource); the dashboard by its `pnpm-lock.yaml`.

## Change management (DPP 2.3.2)

Every change goes through a pull request on GitHub, the local gates (`make api-lint api-typecheck
lint-imports test-unit gitleaks commit-lint`), a code and security review (`docs/PROCESS.md`) and a
human approval before merge. Production changes only through `scripts/deploy/gcp/deploy.sh`, whose
guards refuse unsafe states (ingress, issuer, missing KMS keys). The person who approves a merge is
not the person who wrote it whenever the team has two people available.

## Security awareness training (DPP 1.2.4)

Once a year, and before first access, every Approved User completes a short training (about one
hour): phishing and account takeover, MFA and password managers, handling marketplace data (no
exports to personal devices, no sharing keys or tokens), Amazon's AUP and DPP in brief, and how to
report an incident (`INCIDENT-RESPONSE.md`).

| Date | Person | Course or session | Next due |
|---|---|---|---|
| | | | |
