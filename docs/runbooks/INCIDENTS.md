# Incidents: who, where to look, how to recover (prod)

Purpose: the first page to open when an alert fires or a brand reports that something is stuck.
Deploy steps live in `DEPLOY-GCP.md`; the edge in `EDGE-CLOUDFLARE.md`; staff account steps in
`MFA-STAFF.md`. Never paste a secret, a token or brand data into a ticket or chat.

## Who

| Role | Who | Owns |
|---|---|---|
| Owner | the `ALERT_EMAIL` address in `scripts/deploy/gcp/prod.env` (blank today: the owner fills it in) | Cloud Monitoring mails, GCP access, Secret Manager values, OpenRouter top-ups |
| On-call engineer | named by the owner, one at a time | Sentry issues and Sentry alert rules, the Slack alert channel, the steps below |
| Brand contact | the tenant's owner | reconnecting Amazon, re-approving listings |

The prod worker is meant to be on (`WORKER_ENABLED=true` in `prod.env`, worker pool
`aimos-worker`). A worker that is not running is an incident, never the normal state.

## Where to look

- **Cloud Monitoring** (project `aimos-prod`): the policies `aimos-edge uptime failing`,
  `aimos-worker heartbeat missing`, `aimos-worker restarting`, `aimos-maintenance errors`
  (`DEPLOY-GCP.md` "Alerts and Slack").
- **Logs Explorer**: the api (`resource.type="cloud_run_revision"`, service `aimos-api`), the
  worker pool (`aimos-worker`), the job `aimos-maintenance`. Lines are JSON: search
  `jsonPayload.message`, then follow `request_id` or `job_run_id`. Lines carry ids and codes only.
- **Sentry**: crashes of the api and of job bodies (tagged `job_kind`, `request_id`), and every
  alert kind of `app/core/alerts.py`.
- **Slack** (once `ALERT_SLACK_WEBHOOK` has a value): `llm_budget`, `llm_credit`,
  `llm_provider_refused`, `queue_latency`, `token_refresh_failed`, `job_dead_lettered`.
- **State**: `scripts/deploy/gcp/deploy.sh prod status` (revisions, worker pool, recent job runs);
  `scripts/deploy/gcp/deploy.sh prod smoke`.

## One section per alert

### API or database down (`aimos-edge uptime failing`)

1. `deploy.sh prod smoke`. If `/readyz` says the database is not ok, check the Cloud SQL instance
   `aimos-db` in the console (state, CPU, storage, maintenance).
2. If the last release broke it, roll back the api (below). If Cloudflare is the problem, follow
   `EDGE-CLOUDFLARE.md` (its rollback sets `ORIGIN_AUTH=off`).
3. If the database is lost or corrupted, restore (below). Never restore over the live instance.

### Worker down or hung (`aimos-worker heartbeat missing`, `aimos-worker restarting`)

1. Logs of `aimos-worker` around the last `periodic.alerts_checked`: a `FATAL: refusing to boot`
   line names a missing or bad setting (a secret without a value, a pipeline ref); a crash shows
   in Sentry.
2. Fix the setting, then `deploy.sh prod deploy`. To go back to the previous image, roll back the
   worker (below).
3. Queued jobs wait in `job_runs` and run when the worker is back; the reaper re-queues jobs whose
   lease expired. Nothing needs replaying by hand.

### AI paused (`llm_provider_refused`, `llm_credit`)

1. `GET /api/v1/admin/ai-credit` (a platform admin) shows the pause reason, the last key check and
   today's spend.
2. Credit used up: top up OpenRouter. Key refused: put a new `OPENROUTER_API_KEY` version in Secret
   Manager (the `read -rsp` command `setup` prints) and `deploy.sh prod deploy`.
3. `POST /api/v1/admin/ai-credit/resume` clears the pause at once (the worker also clears it on its
   own when the credit is back above the floor).

### Token refresh failed (`token_refresh_failed`)

The account moves to `needs_reconnect`, and its brand sees a reconnect task. The brand's owner
reconnects Amazon from the dashboard (Marketplaces, Reconnect). Publishes of that account wait
on the task; nothing is lost. Many accounts at once: check `AMAZON_CLIENT_SECRET` and the LWA app
status in Seller Central before asking brands.

### Publish failures (`job_dead_lettered`)

1. Sentry and the logs by `job_run_id` give the cause (`error_code`).
2. Marketplace outage or throttling: wait, then requeue. Code bug: fix, release, then requeue.
3. `python -m app.cli jobs dead-letter list [--tenant SLUG]` and
   `python -m app.cli jobs requeue <job_id> --staff-email E --reason R`, run as the staff CLI in a
   one-off job of the release image (`deploy.sh` has no subcommand for them yet: the `tenant`
   subcommand shows how its job is built).

### Maintenance job errors (`aimos-maintenance errors`)

The job exits 1 when a step failed or the Amazon DPP purge could not finish an account. Read the
`maintenance.step_failed` / `maintenance.step_incomplete` lines, fix the cause, then rerun it:
`gcloud run jobs execute aimos-maintenance --region=asia-south1 --project=aimos-prod --wait`. Every
step is idempotent. An account left unpurged must be purged within the DPP 20-day window.

## Roll back

**Api.** Move traffic to the last good revision (instant, no rebuild):
```bash
gcloud run revisions list --service=aimos-api --region=asia-south1 --project=aimos-prod
gcloud run services update-traffic aimos-api --to-revisions=<GOOD_REVISION>=100 \
  --region=asia-south1 --project=aimos-prod
```

**Worker and maintenance job.** No traffic split: redeploy the previous image,
`scripts/deploy/gcp/deploy.sh prod deploy --sha <previous 12-char sha>` (tags are immutable, so
the image is still there). Migrations are forward-only: roll code back only to a release that
still works on the current schema (expand then contract).

**The AI pipeline or model.** `prod.env` keeps the rollback values beside the live ones:
- `PIPELINE_REF`: the comment above it names the previous commits (today v0.4.2
  `a3d54fbc842f8ad81c954bb50837c23021476471`, only together with reverting the white padding, and
  v0.4.1 `bf6214d6231347ca9a676ebcd9ed493da8477443`). Set the value, commit, `deploy.sh prod release`.
- `LLM_MODEL_DEFAULT`: the comment names the fallback model (`deepseek/deepseek-chat`). Set it,
  commit, `deploy.sh prod deploy`.

## Restore the database (prod commands)

Never over the live instance: clone into a new one, check it, then switch (DEPLOY-GCP.md "Restore
the database" has the switch-over steps). The restore drill (launch gate 6, under 15 minutes) has
not been run yet; time it on the first run.

```bash
# 1. clone to a point in time (UTC) inside the 7-day PITR window
time gcloud sql instances clone aimos-db aimos-db-restore \
  --point-in-time=<YYYY-MM-DDTHH:MM:SSZ> --project=aimos-prod
# or a daily backup into a new instance
gcloud sql backups list --instance=aimos-db --project=aimos-prod
# 2. check it as app_ro through the proxy (a second terminal), never as postgres
cloud-sql-proxy aimos-prod:asia-south1:aimos-db-restore --port 6543
psql "host=127.0.0.1 port=6543 dbname=marketplace_os user=app_ro" -c \
  "SELECT count(*) FROM tenants; SELECT max(created_at) FROM job_runs;"
# 3. sealed tokens are present: kms_key_version is set together with token_enc (app_ro cannot
#    read token_enc itself); decrypting needs the KMS key, which a clone does not change
psql "host=127.0.0.1 port=6543 dbname=marketplace_os user=app_ro" -c \
  "SELECT status, count(*) FILTER (WHERE kms_key_version IS NOT NULL) FROM marketplace_accounts GROUP BY status;"
# 4. a drill ends by deleting the clone
gcloud sql instances delete aimos-db-restore --project=aimos-prod
```

The decrypt check of a restored `token_enc`: only `aimos-worker` may decrypt with the prod key,
so it cannot be run against a throwaway clone. After a real switch-over, the first token refresh
(`marketplace_token_refresh`, every 5 minutes) proves it: watch for `token_refresh_failed`.
Backups sit in `asia-south1`, the same region as the zonal primary: cross-region backups
(asia-south2) wait for the client's decision.
