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
| Pending OAuth rows (state, handle, sealed pending tokens) | 10 minutes valid; deleted daily once expired (hourly when a worker runs) | `prune_oauth_states` in the daily maintenance job (below) | Built: live after the next `setup` and `deploy` |
| Listing state, submissions, raw answers | Life of the listing; deleted 25 days after its marketplace account is revoked or disconnected; otherwise with the tenant purge | `purge_marketplace_account` (migration 0011b) in the daily maintenance job; `purge_tenant` (ADR-0014) | Built: live after the next `release` |
| Brand business data (products, listings, images, brand documents) | Life of the tenant | `purge_tenant` | Live |
| CSV uploads (objects) | 30 days | Bucket lifecycle rule on `aimos-csv-prod` (`CSV_RETENTION_DAYS=30`) | Live |
| Import rows and job history | 90 days (180 for failed jobs) | `retention_daily` (`apps/api/app/repositories/retention.py`) in the daily maintenance job | Built: live after the next `setup` and `deploy` |
| Refresh tokens | 7 days after expiry (a token lives 14 days) | `retention_daily` | Built (maintenance job) |
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

## The daily maintenance job

The prod worker is off (`WORKER_ENABLED=false`) until the AI package lands, so retention does not
depend on it. `deploy.sh deploy` ships the Cloud Run job `aimos-maintenance` on each release's
image and points the Cloud Scheduler trigger `aimos-maintenance-daily` at it (03:00 IST). The job
runs `python -m app.workers.maintenance`: the OAuth state prune, the idempotency-key prune,
`retention_daily`, then the marketplace account purge. Every step is idempotent and a failed step
does not stop the next; the execution is marked failed and Cloud Run retries it once.

- Runtime identity `aimos-maint`: Cloud SQL client and four secrets only (`DATABASE_URL` for the
  retention steps, `ALEMBIC_DATABASE_URL` for the purge, the Cloud Storage HMAC pair for object
  deletes). No other role.
- Scheduler identity `aimos-sched`: `roles/run.invoker` on this one job, nothing else.
- `setup` enables `cloudscheduler.googleapis.com` and creates both identities.
- Check it: `gcloud run jobs executions list --job=aimos-maintenance --region=asia-south1` (also
  in `deploy.sh prod status`), and the `maintenance.done` and `periodic.accounts_purged` log lines.

## Deletion triggers (DPP 1.7)

All marketplace data must be deleted within **30 days** of the earliest trigger.

| Trigger | What happens | Status |
|---|---|---|
| A brand revokes Kuinn inside Kuinn | Tokens deleted at once. 25 days later the daily job deletes the account's listings (with their items, issues, versions, decisions, runs and jobs), its submissions and their stored payloads and raw answers, its tasks and the account row. A reconnect within the 25 days keeps everything | Tokens: Live. Rest: Built |
| A brand removes Kuinn in Seller Central or the Flipkart seller dashboard | The next token refresh fails and the account goes `needs_reconnect`; 25 days later the same purge runs | Built (the refresh itself runs in the worker; until it is on, staff revoke the account in Kuinn when a brand says so) |
| A tenant leaves (contract ends, erasure request) | Staff run the tenant deletion below; everything of the tenant is gone within 8 days (7-day backups and object soft delete) | Live (manual) |
| Amazon or Flipkart asks for deletion, or Kuinn leaves a programme | Revoke every account of that marketplace; the daily job purges them after 25 days, or staff purge at once with `force` (below) | Built |

What stays after an account purge: the brand's products, colourways, images and facts (the brand's
own data, not marketplace data), the audit rows (ids and allow-listed status values only) and one
`marketplace_account.purged` tombstone holding row counts and no seller data. Those go with the
tenant.

An immediate purge (Amazon, Flipkart or the brand asks), as `migrator` like the tenant deletion
below; then delete the returned `object_keys` from the submissions bucket:

```sql
SELECT public.purge_marketplace_account('<tenant uuid>', '<account uuid>', force => true);
```

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
