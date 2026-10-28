# Data retention, deletion, backups and recovery

Amazon DPP 1.7, 1.8, 2.1 and 2.7.4. This page states what the code and the live project do today
(checked on 2026-10-28), not what is planned; planned items are marked **Gap** or **Built**.
`docs/architecture/DATABASE.md` §9 is the design this follows.

## What Kuinn holds from the marketplaces

Kuinn requests only the Amazon Product Listing role and Flipkart's listing APIs. It receives **no
buyer or customer personal data** from either marketplace. What it does hold:

| Data | Where | Origin tag (DPP 1.8) |
|---|---|---|
| OAuth tokens (refresh and access) | `marketplace_accounts.token_enc`, sealed with Cloud KMS envelope encryption, AES-256-GCM (ADR-0018) | `tenant_id`, `marketplace`, the account id |
| Seller id, marketplace, region, granted scopes | `marketplace_accounts` | same |
| Listing state read back (status, ASIN or FSN, issues) | `listing_items`, `listing_issues`, `publish_field_results` | `tenant_id`, `marketplace`, `marketplace_account_id` |
| Submission snapshots and the raw marketplace answers | `listing_submissions` rows; objects `<tenant_id>/submissions/<submission_id>/{payload.json,raw.jsonl}` in the submissions bucket | `tenant_id` prefix, `marketplace` column |

Marketplace answers never reach an AI provider: the generation context reads no marketplace
account, token or response (`apps/api/app/services/generation_context.py`).

## Retention

| Data | Kept | Enforced by | Status |
|---|---|---|---|
| Marketplace tokens | Until the brand revokes, the tenant is offboarded or purged | `revoke` sets `token_enc` to NULL at once (`apps/api/app/services/marketplaces.py`) | Live |
| Pending OAuth rows (state, handle, sealed pending tokens) | 10 minutes valid; deleted hourly once expired | `oauth_states_prune` in the worker (`apps/api/app/workers/periodic.py`) | Built: the prod worker is off (`WORKER_ENABLED=false`) until the AI package lands. Turn it on before the first real seller connects |
| Listing state, submissions, raw answers | Life of the listing; then the tenant purge | `purge_tenant` (ADR-0014) | Live (tenant level); **Gap** at account level, see below |
| Brand business data (products, listings, images, brand documents) | Life of the tenant | `purge_tenant` | Live |
| CSV uploads (objects) | 30 days | Bucket lifecycle rule on `aimos-csv-prod` (`CSV_RETENTION_DAYS=30`) | Live |
| Import rows and job history | 90 days (180 for failed jobs) | `retention_daily` in the worker (`apps/api/app/repositories/retention.py`) | Built: runs once the prod worker is on |
| Refresh tokens | 7 days after expiry (a token lives 14 days) | `retention_daily` | Built (worker) |
| User accounts (email, argon2 password hash, sealed MFA secret, recovery-code hashes) | Until the user or the tenant is erased | `redact_user`, `purge_tenant` (migrator only) | Live |
| `audit_log` (ids only; email and IP are nulled at erasure) | 24 months, never under 12 | `prune_audit_log` (migrator only) | Live (function); first prune due 2028 |
| Cloud Logging `_Default` (app and request logs, redacted) | 30 days | `LOG_RETENTION_DAYS` in `scripts/deploy/gcp/prod.env` | Live |
| Cloud Logging `security-logs` (sign-in events, refused requests, WAF denies) | 400 days | `deploy.sh setup` and `deploy.sh edge alerts` | Built: run `setup` |
| Cloud Logging `_Required` (Cloud Audit Logs: admin activity) | 400 days, fixed by Google | Google | Live |
| Database backups | 7 daily backups and 7 days of point-in-time logs | `--retained-backups-count=7`, `SQL_PITR_DAYS=7` | Live |
| Deleted Cloud Storage objects | Recoverable for 7 days (soft delete), then gone | Bucket soft-delete policy | Live |
| Build sources | 7 days | Bucket lifecycle on the build bucket | Live |
| Container images | The 20 newest; `sha-` tags older than 30 days and untagged images older than 14 days are deleted | Artifact Registry cleanup policy | Live |

Logs never hold a secret, a token, a password, an email or a raw IP: `apps/api/app/core/redaction.py`
filters every log line and every Sentry event, and sign-in events carry only ids and a keyed IP hash
(`apps/api/app/services/auth_events.py`).

## Deletion triggers (DPP 1.7)

All marketplace data must be deleted within **30 days** of the earliest trigger.

| Trigger | What happens today | Status |
|---|---|---|
| A brand revokes Kuinn inside Kuinn | Tokens deleted at once. Listing state and raw answers stay with the listings | Tokens: Live. Rest: **Gap** |
| A brand removes Kuinn in Seller Central or the Flipkart seller dashboard | Kuinn notices at the next token refresh, which fails, and marks the account `needs_reconnect`; staff then revoke it in Kuinn | Built (refresh needs the worker); **Gap** for the rest, as above |
| A tenant leaves (contract ends, erasure request) | Staff run the tenant deletion below; everything of the tenant is gone within 8 days (7-day backups and object soft delete) | Live (manual) |
| Amazon or Flipkart asks for deletion, or Kuinn leaves a programme | Revoke every account of that marketplace in every tenant, then delete that marketplace's listing state and raw answers | Tokens: Live. Rest: **Gap** |

**Gap, account level:** there is no command yet that deletes one marketplace account's listing
state, issues and raw answers while the tenant stays. The tables are append-only for the app role,
so this needs a migrator-only function (like `purge_tenant`, scoped to a marketplace account) plus a
staff command and a daily job that runs it 25 days after a revoke. Until it lands, the honest answer
to Amazon is: tokens are deleted at once, tenant data on request within 30 days, and per-account
listing data stays with the brand's listings until the brand leaves.

## Tenant deletion (manual, works today)

Run by the GCP owner or an engineer with the migrator credential. Record the request id, the
tenant id and each step's time in the incident or request log.

1. In the dashboard or by API, revoke every marketplace connection of the tenant (tokens gone).
2. Open a Cloud SQL Auth Proxy to `aimos-prod:asia-south1:aimos-db` and connect to
   `marketplace_os` as `migrator` (password from Secret Manager `MIGRATOR_PASSWORD`; never paste it
   anywhere else).
3. Offboard and purge (the `force` flag skips the 30-day grace, which an erasure request does not
   need):

   ```sql
   SELECT public.set_tenant_lifecycle('<tenant uuid>', 'offboarded', 'deletion request <ref>');
   SELECT public.purge_tenant('<tenant uuid>', 'data', true);
   ```

   This deletes every tenant table except `tenants`, `tenant_memberships` and `audit_log`, nulls the
   email and IP in the tenant's audit rows and pseudonymises users with no other membership.
4. Delete the tenant's objects:

   ```
   for b in aimos-images-prod aimos-csv-prod aimos-submissions-prod; do
     gcloud storage rm -r "gs://$b/<tenant uuid>/" --project=aimos-prod
   done
   ```

5. Wait out the backups and soft delete (7 days). Then the deletion is complete; confirm in writing
   if Amazon or the brand asks (DPP 1.7). `purge_tenant(t, 'final')` removes the audit rows and the
   tenant row after the 24-month audit retention.

Secure deletion follows NIST 800-88 "purge" through the cloud provider: Cloud SQL and Cloud Storage
data is encrypted at rest, and deleted data becomes unrecoverable when its storage is reclaimed and
its keys expire. Marketplace tokens are additionally crypto-shredded: the sealed blob is useless
without the KMS key, and the column is set to NULL.

## Backups and recovery (DPP 2.7.4)

| Item | Today | Target |
|---|---|---|
| Automated backups | Daily, 7 kept, in asia-south1, kept after instance deletion | Second location: set `SQL_BACKUP_LOCATION` (for example `asia-south2`, Delhi, which keeps data in India) in `prod.env`, then run `deploy.sh prod setup`. Needs the client's agreement on the second region |
| Point-in-time recovery | 7 days of logs | Same |
| RPO | About 5 minutes (PITR) | Same |
| RTO | About 2 hours for logical damage (clone, swap the secret, redeploy); about 4 hours if the zone is lost (zonal instance) | Same; regional HA is a cost decision |
| Restore procedure | `docs/runbooks/DEPLOY-GCP.md`, "Restore the database"; local drill `make drill-restore` | A restore drill into a scratch instance every quarter, recorded below |
| Code and configuration | Git (GitHub) and `scripts/deploy/gcp/`; the whole project can be rebuilt with `deploy.sh setup` and `release` | Same |
| KMS keys | Key versions are never destroyed while a backup may use them | Same |

| Date | Restore drill | Result |
|---|---|---|
| | | |
