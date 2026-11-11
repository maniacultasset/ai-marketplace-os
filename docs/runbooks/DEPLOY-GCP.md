# Deploy to GCP by hand (staging and prod)

`scripts/deploy/gcp/deploy.sh <staging|prod> <subcommand>` stands up and deploys the backend on
the ADR-0001 layout: one image on Cloud Run (api service, worker pool, jobs), Cloud SQL PostgreSQL
17, GCS through the S3-compatible API, Secret Manager and Cloud KMS, all in `asia-south1`. A person
runs it in their own terminal, under their own `gcloud auth login`. Settings that are not secret
live in `scripts/deploy/gcp/staging.env` and `prod.env`; every secret lives only in Secret Manager.

This is the by-hand exception to "reviewed OpenTofu only" (STATE "GCP by hand";
`docs/EXECUTION-PLAN.md` §5.1). At S4, `tofu import` what it made (the list is at the end) and
change it only through code afterwards.

Every subcommand describes before it creates, so a rerun is safe. It prints each `gcloud` command
before running it. `--dry-run` prints the commands and runs none. No secret value is printed,
passed on a command line or written to a committed file.

## Subcommands

| Subcommand | What it does |
|---|---|
| `preflight` | Checks `gcloud`, the account, that the config project is the env's `PROJECT`, billing, and the API list. |
| `setup` | One time, rerunnable. Enables the APIs and sets log retention, and creates the 400-day `security-logs` bucket with the `app-security` sink (the api's `auth.*` events and its 401/403/429 requests, never an OAuth callback URL; Amazon DPP 2.6.1). Creates the Artifact Registry repo (immutable tags, cleanup policy), the service accounts, the KMS key ring `marketplace-os` with the keys `oauth-token-key` and `mfa-secret-key` (90-day rotation each; only the api service account may use `mfa-secret-key`, ADR-0021), the buckets (uniform access, public access prevention, CSV lifecycle, CORS for the dashboard origins) and an HMAC key into `S3_ACCESS_KEY`/`S3_SECRET_KEY`. Enables Cloud Scheduler and creates the maintenance identities `aimos-maint` (Cloud SQL client, `DATABASE_URL`, `ALEMBIC_DATABASE_URL` and the HMAC pair) and `aimos-sched` (no project role). Creates Cloud SQL and the database (backups in `SQL_BACKUP_LOCATION`, default the region; on a live instance a different value moves new backups there), the generated secrets and the per-secret IAM. Stops if `SENTRY_DSN` (or, with the worker on, its model key: `OPENROUTER_API_KEY` under `LLM_GATEWAY=openrouter`; `OPENAI_API_KEY`, plus `DEEPSEEK_API_KEY` under `LLM_PROVIDER=deepseek`, under `direct`; `GOOGLE_OAUTH_CLIENT_SECRET` with Google sign-in on; `AMAZON_CLIENT_SECRET` with `AMAZON_APP_ID` set) has no value, and prints the command to add it. With `PIPELINE_REF` set (ADR-0024) it also creates `MKOS_PIPELINE_DEPLOY_KEY`, readable by `aimos-build` alone, and stops until it has a value. |
| `build` | Cloud Build of `apps/api/Dockerfile` from a `git archive` of a clean `HEAD`, pushed as `.../aimos/backend:sha-<12>`. Refuses uncommitted changes. Skips a tag that already exists. `PIPELINE_REF` empty: `cloudbuild.yaml`, the in-repo stub image. `PIPELINE_REF` set: `cloudbuild-pipeline.yaml`, the AI team's `mkos-pipeline` at that commit (see "The real pipeline" below); refuses first when `MKOS_PIPELINE_DEPLOY_KEY` has no value. |
| `db-init` | One time, rerunnable. A one-off job of the same image (`APP_ROLE=shell`) runs `scripts/db/run_bootstrap.py` as `postgres` over the `/cloudsql` socket. It runs `0000_bootstrap.sql`, then sets the `app_rw`, `app_ro` and `migrator` passwords from SCRAM verifiers, under the `0000_role_passwords.sql` safety checks. |
| `migrate` | The migrate job (`APP_ROLE=migrate`, all as `migrator`): `alembic upgrade head`, the dictionary sync, `rules load` (the platform rules), then `templates load` (the Amazon and Flipkart mappings), each from the files in the image and each idempotent, so a rerun writes nothing. Rules load before templates, so a failed run never leaves a category generatable without its rules. The error map is not loaded: it stays empty until observed rows exist. A rule taken out of `rules.seed.json` stays stored and enabled: to retire one, keep it in the file with `"enabled": false`. Once the rules are loaded, validation, review, approval and the image gate evaluate them for every tenant, so results change for existing listings. It runs with `max-retries 0`, waits, and fails when the run fails. |
| `deploy` | The api service: public, the app does its own auth, Cloud SQL attached, secrets as env vars. A new revision gets 0% traffic until its tagged URL passes `smoke`, then takes all traffic; then every `sha-*` revision tag is removed, its own included, because a tag URL reaches that revision directly and would bypass settings a later revision changed (MFA, lockout). Roll back by revision name (`update-traffic --to-revisions`). On the first deploy, `JWT_ISSUER` and the OAuth redirect URIs are re-pointed to the URL the service got (a pinned `JWT_ISSUER` in the env file stays as set). Ingress comes from `INGRESS` (`all`, or `internal-and-cloud-load-balancing` once the edge is locked; the locked value needs `API_PUBLIC_URL=https://$EDGE_DOMAIN`). With ingress locked, the new revision is tagged `candidate` and smoked through the load balancer with the header `x-aimos-candidate` set to the random value `edge setup` wrote into the URL map (never printed); the tag is removed on every exit, and a failed smoke keeps traffic. A KMS guard (after the live-state guard below) checks that `oauth-token-key` and `mfa-secret-key` exist and stops with "run setup first" when one is missing (a release that added a key must run `setup` before `deploy`). A live-state guard first reads the running service (any describe error but NOT_FOUND stops the deploy) and refuses to unlock a locked service (`DEPLOY_ALLOW_UNLOCK=1` overrides), to lock an open one, which is `edge lock-ingress`'s job (`DEPLOY_ALLOW_LOCK=1` overrides), or to change its `JWT_ISSUER`, which signs every user out (`DEPLOY_ALLOW_ISSUER_CHANGE=1` overrides). Then the worker pool (`WORKER_ENABLED=true` only), then the daily maintenance job `aimos-maintenance` on the same image (`python -m app.workers.maintenance`: OAuth state and idempotency-key prunes, `retention_daily`, the marketplace account purge; `aimos-sched` may run it via `roles/run.invoker` on the job) and its Cloud Scheduler trigger `aimos-maintenance-daily` (`MAINT_SCHEDULE`, `MAINT_TIME_ZONE`; created once, updated after), then `smoke`. |
| `release` | The KMS guard and the setup guard, then `build`, `migrate`, `deploy`: the normal update. The setup guard stops with "run setup first" before any build when `aimos-maint` or `aimos-sched` is missing or `cloudscheduler.googleapis.com` is off (a project whose `setup` predates the maintenance job); `deploy` runs both guards too. The model key guard (ADR-0024) stops the same way when the worker is on and one of its model keys (the gateway's) has no value. The google and amazon guards stop the same way when their switch is on and `GOOGLE_OAUTH_CLIENT_SECRET` or `AMAZON_CLIENT_SECRET` has no value. After pulling a release that adds a key, an account or an API, run `setup` before `release`. |
| `smoke` | `GET /healthz` must answer 200 (on a `*.run.app` URL, whose front end answers `/healthz` itself with a 404, and on the edge domain, whose Cloud Armor path rule denies it, `GET /api/v1/auth/me` must answer 401 instead); `GET /readyz` must answer 200 with `"postgres":"ok"`. Retried for cold starts. |
| `restore-drill` | Launch gate 6, run and timed ("Restore drill" below): `restore-drill [--pitr YYYY-MM-DDTHH:MM:SSZ] [--keep]` restores the latest successful backup (or a point in time) of `SQL_INSTANCE` into a new throwaway `<SQL_INSTANCE>-drill-<ddHHMMSS>`, checks it read only through `cloud-sql-proxy`, times every phase against 15 minutes, then deletes it (also on any failure). Never creates, patches, restores into or deletes `SQL_INSTANCE`. `--dry-run` prints every command and runs none. |
| `tenant` | `tenant create --name N --slug S --owner-email E --staff-email F [--trial-days D]`, `tenant add-member --slug S --email E --role owner\|operator\|reviewer\|admin --staff-email F`, `tenant set-password --email E --staff-email F`, `tenant make-admin --email E --staff-email F`, and (DPP-1, after the identity check in `MFA-STAFF.md`) `tenant unlock --email E --staff-email F` (no password) and `tenant reset-mfa --email E --staff-email F`. Runs the staff CLI (`python -m app.cli`) in a one-off job. A new user's password is generated into `INITIAL_PASSWORD_<email>_<hash>` (8 groups of an upper-case letter, a lower-case letter and a digit, so it meets the DPP rules; must-change at the next sign-in), and the job reads it on stdin from a mounted file. The script prints the `gcloud secrets versions access` command that shows it, never the value. A rerun of `create` with a taken slug fails and changes nothing. |
| `templates` | `templates refresh --product-type T --staff-email E --reason R` and `templates promote-definition --product-type T --version V [--digest D] --staff-email E --reason R` (DW2, `PTD-REFRESH.md`). Runs `python -m app.cli templates ...` in the one-off job `aimos-ptd` under its own identity `aimos-ptd` (secret access is per identity, so the shared ops identity never gets the token), with the Amazon app's public settings, `AMAZON_CLIENT_SECRET`, `AMAZON_PLATFORM_REFRESH_TOKEN` and the migrator URL. Only while `AMAZON_APP_ID` is set. The platform token is a self-authorization of the Kuinn app on the company's own seller account; the owner adds it with the printed `read -s` command. Only this job mounts it. |
| `amazon-check` | `amazon-check (--slug S \| --seller-id ID) --staff-email E [--ean EAN] [--asin ASIN]`. The read-only Amazon connection check of one tenant's live account: runs `python -m app.cli ops amazon-check` in the one-off job `aimos-amazon-check` (it resolves the slug, or by `--seller-id` the one tenant whose connected Amazon account has that seller id, refusing when none or several match; it prints the tenant slug with the seller id masked, then hands the process to the worker entrypoint `python -m app.workers.amazon_check`) as the worker identity (it already opens tenant tokens with the token key, and reads `DATABASE_URL` and `AMAZON_CLIENT_SECRET`), under the shell role. Steps: the account and its KMS-sealed token, a fresh LWA token (kept in memory, the row is unchanged), getMarketplaceParticipations (Amazon.in), getDefinitionsProductType for KURTA and SHIRT against the stored versions, a Listings probe (404 expected), searchCatalogItems by the EAN, getListingsRestrictions for the first ASIN, and the rate-limit headers. Every SP-API request goes through a transport that refuses anything but GET; the LWA refresh is the one POST. One audit row names the staff email. The job log holds a PASS / WARN / FAIL table with no token, payload or full seller id; a FAIL row fails the job. Only while `AMAZON_APP_ID` is set. |
| `amazon-preview` | `amazon-preview --slug S --listing-id L --staff-email E [--precheck]`. The owner's one-off Amazon dry run of one listing (decision of 2026-11-12; runbook `AMAZON-PREVIEW-ONEOFF.md`): runs `python -m app.cli ops amazon-preview` (it resolves the slug, then hands the process to `python -m app.workers.amazon_preview`) in the one-off job `aimos-amazon-preview` as the worker identity, under the shell role, with the secrets `amazon-check` uses (`DATABASE_URL`, `AMAZON_CLIENT_SECRET`). Only this job's env carries `AMAZON_PUBLISH_ENABLED=true`, `AMAZON_VALIDATION_PREVIEW_ENABLED=true`, `AMAZON_CATALOG_PRECHECK_ENABLED` (true only with `--precheck`) and `AMAZON_PREVIEW_ONEOFF=1`; `prod.env`, the api and the worker are untouched, and every role but the shell refuses to boot with `AMAZON_PREVIEW_ONEOFF`. The job is executed once and waited for, its log printed, then deleted. Every SP-API request is a GET or a `VALIDATION_PREVIEW` PUT (anything else is refused before it is sent); no job is enqueued and no verdict is written. A SKU that exists on Amazon, or a reply that looked applied, prints STOP and fails the command. Only while `AMAZON_APP_ID` is set. |
| `images` | `images recheck (--tenant ID \| --slug S \| --seller-id ID) --staff-email E`. Photo rules 2.1.0: queues the photo checks again for one tenant's live photos whose stored report the old photo rules wrote. Runs `python -m app.cli images recheck` in the one-off job `aimos-images-recheck` as the worker identity, under the shell role, with `DATABASE_URL` only (app_rw, tenant scope); no model key, no Amazon secret. `--slug` and `--seller-id` resolve the tenant as `amazon-check` does (the one tenant whose connected Amazon account has that seller id; refused when none or several match; the seller id is masked in the log). Each run queues at most the free per-brand photo job cap and writes one `images.recheck` audit row naming the staff email; the job log ends `images recheck: ok (queued=N remaining=M)`. Rerun it, after the worker drains the queued checks, until `remaining=0`. A listing's photo issues follow on its next check (a read of a reviewable listing, "Check again" or a regenerate). Nothing is approved, sent or published. |
| `sentry-alerts` | The Sentry issue alert rules as code (`sentry_rules.py`; "Sentry alert rules" below), with the API token from the `SENTRY_ALERTS_TOKEN` secret piped to the helper's stdin. Needs `SENTRY_ORG`, `SENTRY_PROJECT` and `SENTRY_API_URL` in the env file; refused when any is blank or malformed. `--dry-run` prints the four rules and contacts neither Google Cloud nor Sentry. |
| `status` | The service URL, latest revisions, worker pool, recent job runs and secret names (never values). |
| `edge` | Prod only (needs `EDGE_DOMAIN`; staging is refused). `edge setup` builds the global external load balancer for `api.kuinn.in` (managed certificate, SSL policy MODERN / TLS 1.2, serverless NEGs for the service and its `candidate` tag, logged backends with the Cloud Armor policy `kuinn-api-waf`); `edge rules` converges the policy to `armor-rules.yaml` rule by rule, every group in preview unless listed in `EDGE_ENFORCE`; `edge enforce` does the same after printing what leaves preview; `edge lock-ingress` / `edge unlock-ingress` close and reopen the `run.app` URLs; `edge alerts` sets up the uptime, worker and maintenance alerts (see "Alerts and Slack") plus the deny/429/5xx/Adaptive Protection alerts to `ALERT_EMAIL` and the 400-day `security-logs` bucket; `edge cors` resets the bucket CORS from `ALLOWED_ORIGINS`; `edge status` prints the read-only state and evidence. Order, gates and rollbacks: `docs/runbooks/EDGE-CUTOVER.md`. Interim (ADR-0025): with `EDGE_PROVIDER=cloudflare` only `edge alerts` (the provider-independent part, below), `edge cors` and `edge status` run, and the api gets the Worker's `EDGE_ORIGIN_SECRET`; see `docs/runbooks/EDGE-CLOUDFLARE.md`. |

`--sha <12 hex>` points `db-init`, `migrate`, `deploy`, `smoke`, `tenant` and the one-off jobs
(`templates`, `amazon-check`, `amazon-preview`, `images`) at an image other than `HEAD`'s.

## First deploy, in order (staging; prod is the same with `prod`)

1. Billing is on for the project, and you are signed in: `gcloud auth login`, then
   `gcloud config set project aimos-staging`. EXECUTION-PLAN §5.1 recommends a dedicated
   `CLOUDSDK_CONFIG`.
2. In `scripts/deploy/gcp/staging.env`, set `ALLOWED_ORIGINS` and `DASHBOARD_BASE_URL` (the
   dashboard's https origins). Commit the change: `build` refuses a dirty tree.
3. `scripts/deploy/gcp/deploy.sh staging preflight`
4. `scripts/deploy/gcp/deploy.sh staging setup`. Cloud SQL creation takes 10 to 20 minutes. When it
   stops for `SENTRY_DSN` or `OPENAI_API_KEY`, paste the `bash -c 'read -rsp ...'` line it printed,
   type the value at the hidden prompt, and rerun `setup`.
5. `scripts/deploy/gcp/deploy.sh staging build`
6. `scripts/deploy/gcp/deploy.sh staging db-init`
7. `scripts/deploy/gcp/deploy.sh staging migrate`
8. `scripts/deploy/gcp/deploy.sh staging deploy`. It ends with the smoke result.
9. `scripts/deploy/gcp/deploy.sh staging tenant create --name "Pilot" --slug pilot --owner-email
   owner@brand.example --staff-email you@yourco.example`. Then read the initial password once
   with the command it prints, and hand it over out of band.
   First platform admin (ADR-0019): `tenant create --name "Staff" --slug staff --owner-email admin@yourco.example --staff-email you@yourco.example`, then `tenant make-admin --email admin@yourco.example --staff-email you@yourco.example`. That account logs in and approves sign-up requests.
10. `scripts/deploy/gcp/deploy.sh staging status`

After that, every update is `deploy.sh staging release`.

## What you provide

- `ALLOWED_ORIGINS` and `DASHBOARD_BASE_URL` in the env file: https origins, no trailing `/`.
- `SENTRY_DSN`, plus `OPENAI_API_KEY` wherever the worker runs: typed at the hidden prompt, straight
  into Secret Manager.
- With `GOOGLE_OAUTH_ENABLED=true` (ADR-0020; off by default): `GOOGLE_OAUTH_CLIENT_ID` in the env
  file and `GOOGLE_OAUTH_CLIENT_SECRET` typed at the hidden prompt. The secret is bound to the api
  only while the switch is on, and `deploy` and `release` refuse to start when it has no value
  (`google guard`). Switching, verification and rollback: `GOOGLE-SIGNIN.md`.
- The Amazon app (off by default; `docs/EXTERNAL-SETUP.md`): `AMAZON_APP_ID` is the switch. Blank,
  Amazon connect answers 501 and no secret is bound. Set (`amzn1.sp.solution.<uuid>`), it needs
  `AMAZON_CLIENT_ID` and an https `AMAZON_AUTHORIZE_URL` in the env file (`deploy.sh` refuses
  anything else before any gcloud call) and `AMAZON_CLIENT_SECRET` typed at the hidden prompt.
  The api (connect) and the worker (token refresh) get all four, never a job; `deploy` and
  `release` refuse while the secret has no value (`amazon guard`). The env files ship
  `?version=beta` on the consent URL, which a draft app needs; drop it once the app is published.
  Change the switch only in the env file plus a same-image redeploy.
- The model gateway in the env file (ADR-0024; the AI team picks the model): `LLM_GATEWAY`,
  `LLM_PROVIDER`, `LLM_MODEL_DEFAULT` and the daily caps `LLM_DAILY_BUDGET_USD` and
  `LLM_TENANT_DAILY_BUDGET_USD`.
  - `LLM_GATEWAY=openrouter` (prod): every worker model call (generation, vision, embedding)
    goes to OpenRouter with one key, `OPENROUTER_API_KEY`, typed at the hidden prompt. The URL is
    fixed in code; there is no URL to set. `LLM_MODEL_DEFAULT` must be an OpenRouter model id
    (`vendor/model`, such as `deepseek/deepseek-chat`): `deploy.sh` refuses a blank or bare one
    before any gcloud call, and the worker refuses it at boot. No OpenAI or DeepSeek key is needed.
  - `LLM_GATEWAY=direct` (staging, and the default): OpenAI's and DeepSeek's own APIs with
    `OPENAI_API_KEY` (and `DEEPSEEK_API_KEY` under `LLM_PROVIDER=deepseek`), as before.
  Only the worker gets the gateway, provider, model and key; the caps go to the api too. After
  switching the gateway with the worker on, rerun `setup` and enter the new key; until then
  `deploy` and `release` refuse (`model key guard`). Each `setup` also removes the worker's
  access to the model keys the gateway no longer uses (before ADR-0024 every worker could read
  `OPENAI_API_KEY`, prod included); the secrets and their values stay.
- Optional: `API_PUBLIC_URL`, once a domain fronts the API. `JWT_ISSUER` and the OAuth redirect URIs
  follow it, and the Amazon/Flipkart applications must list the same callback strings.
- Everything else is generated: `JWT_SECRET`, `ADMIN_SESSION_SECRET`, the `postgres`, `app_rw`,
  `app_ro` and `migrator` passwords, `DATABASE_URL` (`app_rw` over
  `host=/cloudsql/<PROJECT>:<REGION>:<INSTANCE>`), `ALEMBIC_DATABASE_URL` (`migrator`), and the
  HMAC pair.

## Who reads what (least privilege)

| Account | Project roles | Resource roles |
|---|---|---|
| `aimos-api` | `cloudsql.client` | accessor on its 6 secrets (plus `GOOGLE_OAUTH_CLIENT_SECRET` with Google sign-in on, and `AMAZON_CLIENT_SECRET` with `AMAZON_APP_ID` set); `cryptoKeyEncrypter` (encrypt only, ADR-0018) on the one key |
| `aimos-worker` | `cloudsql.client` | accessor on its 5 secrets (the 4 shared ones plus its model key: `OPENROUTER_API_KEY` under `LLM_GATEWAY=openrouter`; `OPENAI_API_KEY` under `direct`, 6 with `DEEPSEEK_API_KEY` under `LLM_PROVIDER=deepseek`; plus `AMAZON_CLIENT_SECRET` with `AMAZON_APP_ID` set), once `setup` has run on this release: it removes the worker's access to the other model keys; `cryptoKeyEncrypterDecrypter` on the key |
| `aimos-migrate` | `cloudsql.client` | accessor on `ALEMBIC_DATABASE_URL` |
| `aimos-ops` | `cloudsql.client` | accessor on the 4 DB passwords, `ALEMBIC_DATABASE_URL`, each `INITIAL_PASSWORD_*` |
| `aimos-storage` | none | `storage.objectAdmin` on the 3 app buckets: the identity behind the HMAC key |
| `aimos-build` | `logging.logWriter` | `artifactregistry.writer` on the repo; `objectViewer` on the build-source bucket; with `PIPELINE_REF` set, accessor on `MKOS_PIPELINE_DEPLOY_KEY` (no other account reads it) |

## The real pipeline (ADR-0024)

CI, local runs and every image built with `PIPELINE_REF` empty carry the in-repo
`mkos-pipeline-stub`. A production worker refuses the stub at boot, so prod runs no worker until
the image carries the AI team's `mkos-pipeline`. `deploy.sh` refuses `WORKER_ENABLED=true` in
production while `PIPELINE_REF` is empty.

1. The AI team names the commit to ship. Put its full 40-character sha in `PIPELINE_REF` in the
   env file (a tag is not accepted; note it in the commit message) and commit.
2. `scripts/deploy/gcp/deploy.sh prod setup`. It creates `MKOS_PIPELINE_DEPLOY_KEY` with no value,
   grants `secretAccessor` on it to `aimos-build` only, and stops with the command to run in your
   own terminal: it makes an ed25519 key in a temp dir, adds the public half to
   `maniacultasset/mkos-pipeline` as a read-only deploy key (`gh repo deploy-key add`, no
   `--allow-write`), stores the private half with `gcloud secrets versions add ... --data-file=`,
   and deletes both files. Rerun `setup`.
3. `build` (or `release`). Cloud Build runs `cloudbuild-pipeline.yaml`: the key goes from Secret
   Manager to a BuildKit secret mounted for one `RUN`; the Dockerfile fetches exactly that commit
   over SSH with GitHub's ed25519 host key pinned, uninstalls the stub and installs
   `mkos-pipeline` with `--no-deps` (it keeps the image's `mkos-schemas`, `httpx` and `pydantic`).
   The build fails unless `mkos_pipeline` imports from `mkos-pipeline` with no stub installed. The
   image carries the label `mkos.pipeline.revision`.
4. Only then set `WORKER_ENABLED=true` in `prod.env`, commit, run `setup` and add the one key
   it asks for (`OPENROUTER_API_KEY`, since `prod.env` says `LLM_GATEWAY=openrouter`), then
   `release`.

Rotate the deploy key by adding a new deploy key and secret version, then deleting the old deploy
key on GitHub. Revoke it by deleting the deploy key on GitHub; builds with `PIPELINE_REF` set fail
until a new one is added, and the stub build never needs it.

## Roll back a bad revision

```bash
gcloud run revisions list --service=aimos-api --region=asia-south1 --project=aimos-staging
gcloud run services update-traffic aimos-api --to-revisions=<GOOD_REVISION>=100 \
  --region=asia-south1 --project=aimos-staging
```
The next `deploy` (or `--to-latest`) moves traffic forward again. The worker pool has no traffic
split: redeploy the previous image with `deploy.sh staging deploy --sha <previous sha>` (tags are
immutable, so that image is still there). Migrations are forward-only. Roll the code back only when
the older revision still works on the current schema, which is the expand-then-contract rule.

## Alerts and Slack (V1 hardening #1, #2)

Every app alert is sent by the worker, so a dead worker alerts nobody by itself. Cloud Monitoring
covers that, with an email channel that does not depend on the edge provider or on the worker.

**Email alerts.** `ALERT_EMAIL` in `prod.env` is blank: the owner fills in one address (x@y.z),
commits, then runs `scripts/deploy/gcp/deploy.sh prod edge alerts`. With
`EDGE_PROVIDER=cloudflare` it enables `monitoring.googleapis.com` when it is off and creates, once
each. Every policy file carries the label `kuinn_spec`, a hash of its conditions, thresholds and
channels: a rerun changes nothing, except the channel's address when `ALERT_EMAIL` changed and, in
place, each policy whose `kuinn_spec` differs (a threshold, a filter or a channel changed; a policy
made before the label is updated once). `--dry-run` prints every command and every policy file and
runs none:

| What | Fires when |
|---|---|
| email channel `aimos-edge-alerts` | (to `ALERT_EMAIL`) |
| uptime check `aimos-edge-readyz` on `https://api.kuinn.in/readyz`, every 5 min; policy `aimos-edge uptime failing` | `/readyz` fails from 2 or more regions for 5 minutes (API or database down) |
| log metric `worker_alerts_checked`; policy `aimos-worker heartbeat missing` | no `periodic.alerts_checked` line from the worker pool for 15 minutes (worker down, hung or never started) |
| log metric `worker_started`; policy `aimos-worker restarting` | more than 2 `worker.started` lines in 30 minutes (a crash loop) |
| policy `aimos-maintenance errors` | any ERROR line of the `aimos-maintenance` job (a failed step or an incomplete DPP purge; at most one mail an hour) |
| policy `aimos-worker job lag` | the worker logged `alert.queue_latency` (a queue's oldest job waited past its limit; at most one mail an hour), so job lag reaches email even with no Slack |
| policy `aimos-db cpu high` | Cloud SQL `aimos-db` CPU over 0.8 for 10 minutes (`SQL_CPU_ALERT`) |
| policy `aimos-db disk filling` | Cloud SQL disk over 0.85 for 10 minutes (`SQL_DISK_ALERT`; storage auto-increase is on, so this means it is growing fast or hit its cap) |
| policy `aimos-db connections high` | more than 80 % of `SQL_MAX_CONNECTIONS` Postgres connections for 5 minutes (`SQL_CONN_ALERT_PCT`) |
| policy `aimos-api 5xx` | more than 10 api 5xx answers in 5 minutes, from Cloud Run's own request count (`API_5XX_ALERT`; a count, not a ratio, so low traffic does not page on one error) |

The thresholds in brackets are process env, not env-file keys (like `EDGE_DENY_ALERT`): for
example `SQL_CONN_ALERT_PCT=70 scripts/deploy/gcp/deploy.sh prod edge alerts`. A value that is not
a number is refused before any gcloud call.

On the load balancer path (`EDGE_PROVIDER` blank) the same command adds the deny, 429, 5xx and
Adaptive Protection policies and the security log bucket. The two worker metrics filter on
`resource.type="cloud_run_worker_pool"`; at the first run, check in Logs Explorer that the
worker's lines carry that resource type, and fix the filter if Google names it otherwise.

**Slack for these policies (optional).** Cloud Monitoring cannot post to the worker's incoming
webhook (`ALERT_SLACK_WEBHOOK` below): Slack refuses its message format. It has its own Slack
channel type, which needs Google's Slack app authorised once in the console, so the toolkit finds
that channel and never creates it:
1. Console, project `aimos-prod`: Monitoring > Alerting > Edit notification channels.
2. Slack > Add new. Sign in to the team's Slack workspace and allow the Google Cloud Monitoring app.
3. Pick the alerts channel (for example `#kuinn-alerts`), set the display name `aimos-ops-slack`,
   Save. In Slack, invite the app to that channel.
4. Set `ALERT_SLACK_CHANNEL=aimos-ops-slack` in `prod.env`, commit, run
   `scripts/deploy/gcp/deploy.sh prod edge alerts`. Every policy gets both channels; policies made
   earlier are updated in place. A name that matches no Slack channel (or more than one) is refused
   before anything changes. Blank it to go back to email only.

**Slack.** `ALERT_SLACK_WEBHOOK` is an optional secret: `setup` creates it with no value and grants
it to the worker, prints how to add a value, and never stops for it. `deploy` binds it to the
worker only once it has an enabled version, so an empty secret never fails a deploy. The worker
posts the kinds in `SLACK_KINDS` (`app/core/alerts.py`): `llm_budget`, `llm_credit` (the OpenRouter
credit at 50/80/95 %), `llm_provider_refused` (AI paused), `queue_latency`,
`token_refresh_failed` and `job_dead_lettered` (each dead letter or failed publish, deduped per
job per day, at most 20 per job kind per 15 minutes, then one summary). To turn it on: make an
incoming webhook in the team's Slack, add it with the `read -rsp` command `setup` prints, then
`deploy.sh prod deploy` (or the next `release`). Every alert kind also reaches Sentry.

**Who owns what.** The owner of `ALERT_EMAIL` owns the Cloud Monitoring policies above. The
Sentry alert rules are code (`sentry-alerts`, next section); the on-call engineer named in
`docs/runbooks/INCIDENTS.md` owns them and the issues they raise.

## Sentry alert rules

`scripts/deploy/gcp/deploy.sh prod sentry-alerts` makes Sentry mail people. It owns four issue
alert rules, all on the `production` environment (`APP_ENV`), each mailing the issue owners and,
when there are none, every active member of the project:

| Rule | Fires when |
|---|---|
| `kuinn: new issue` | an issue is seen for the first time (at most one mail per issue per 30 min) |
| `kuinn: regression` | a resolved issue comes back |
| `kuinn: issue spike` | one issue is seen more than 100 times in an hour |
| `kuinn: ops alert repeats` | an app alert (`alert_kind` tag) `llm_provider_refused`, `llm_credit`, `token_refresh_failed` or `job_dead_lettered` is new, regressed or seen again within the hour (at most one mail per issue per hour) |

Each rule is described before it changes: absent, it is created; present but different in a
field the helper sets, it is updated in place; the same, it is kept. Rules with any other name
are never changed or deleted, so a rule made by hand in Sentry stays. Two rules with the same
`kuinn: ` name stop the run before any change.

**One-time setup (the owner).**
1. In Sentry, note the organisation and project slugs from the project's URL
   (`https://<org>.sentry.io/projects/<project>/`) and the region from the `SENTRY_DSN` host
   (`o<n>.ingest.de.sentry.io` means `https://de.sentry.io`; `.us.` or none means
   `https://sentry.io`). Set `SENTRY_ORG`, `SENTRY_PROJECT` and `SENTRY_API_URL` in `prod.env` and
   commit.
2. Make a token with the scopes `alerts:read` and `alerts:write` only: Settings > Developer
   Settings > Custom Integrations > Create New Integration > Internal Integration, name
   `kuinn-alert-rules`, Permissions: Alerts Read & Write, everything else No Access, Save, then
   copy the token. (A personal token under User Settings > Personal Tokens with the same two
   scopes works too.)
3. `scripts/deploy/gcp/deploy.sh prod sentry-alerts --dry-run` prints the rules. The first live
   run creates the secret `SENTRY_ALERTS_TOKEN` (no value) and stops, printing the `read -rsp`
   command that adds the token without echoing it. Add it, then run
   `scripts/deploy/gcp/deploy.sh prod sentry-alerts` again.

No deployed process reads `SENTRY_ALERTS_TOKEN`: no service account is granted it, and only a
person with Secret Manager access in `aimos-prod` can run this. The token goes from
`gcloud secrets versions access` into the helper's stdin, never argv, a file or the output. To
rotate it: delete the integration's token in Sentry, make a new one, add it as a new version.

**Slack (optional).** With Sentry's Slack integration installed (Settings > Integrations > Slack),
set `SENTRY_SLACK_WORKSPACE` (the integration's numeric id, in its settings URL) and
`SENTRY_SLACK_CHANNEL` (for example `#kuinn-alerts`) together, then rerun: every rule also posts
there. Blank, the rules only mail.

**If the API is not an option** (no token can be issued), make the same four rules by hand:
Alerts > Create Alert > Issues, environment `production`, the "when" and "if" lines from the
table above (for the ops rule: "The event's tags match `alert_kind` equals ..." once per kind,
"any"), action "Send a notification to Suggested Assignees, falling back to All project
members", the action interval from the table, and the rule name exactly as in the table, so a
later `sentry-alerts` run adopts them instead of adding copies.

## Restore the database (point-in-time recovery)

PITR keeps `SQL_PITR_DAYS` (7) of logs. Restore into a new instance, never over the live one
(the prod commands, the checks and the drill: `INCIDENTS.md` "Restore the database"):
```bash
gcloud sql instances clone aimos-db aimos-db-restore --point-in-time=<YYYY-MM-DDTHH:MM:SSZ> \
  --project=aimos-prod   # or aimos-staging
```
Check the clone through `cloud-sql-proxy` as `app_ro`. The roles and their passwords come with the
clone. To switch over:
1. Set `SQL_INSTANCE=aimos-db-restore` in the env file.
2. Disable the current `DATABASE_URL` and `ALEMBIC_DATABASE_URL` versions:
   `gcloud secrets versions disable <n> --secret=...`.
3. Rerun `setup`: it rebuilds both URLs for the new instance, and changes nothing else that exists.
4. Run `deploy`.

A daily backup can also be restored with `gcloud sql backups restore`. The live instance has
deletion protection, and its backups are kept after deletion (`--retain-backups-on-delete`).

### Restore drill

`scripts/deploy/gcp/deploy.sh prod restore-drill` proves the backups restore, and times it (launch
gate 6: restored and checked in under 15 minutes). It needs `cloud-sql-proxy` v2 and `psql` on
`PATH` (`brew install cloud-sql-proxy libpq`), and the runner's own gcloud login with Cloud SQL
Admin and Secret Manager access in the project. In order:
1. Reads the source (`databaseVersion`, tier, disk size) and its latest successful backup. With
   `--pitr <UTC time>` it clones to that time instead (inside the 7-day PITR window), then takes
   deletion protection off the clone first, then its backups and point-in-time recovery (a refusal
   there is reported and the drill goes on; the clone is deleted at the end anyway).
2. Creates `<SQL_INSTANCE>-drill-<ddHHMMSS>` (same version and tier, at least the same disk, no
   backups, no deletion protection, label `purpose=restore-drill`) and restores the backup into it.
   A name already in use, or a target without the label, stops the drill before any restore.
3. Through `cloud-sql-proxy --gcloud-auth` on `127.0.0.1:6544` (`DRILL_PORT`), every session read
   only, the passwords from `APP_RO_PASSWORD` and `MIGRATOR_PASSWORD` in a 0600 pgpass file of the
   work dir (removed on exit): the `alembic_version` revision (as `migrator`, the only role that
   may read it) must be one this checkout knows; row counts of the core tables as `app_ro`, with at
   least one tenant; a smoke: forced row level security still on, every `active` Amazon account
   still has its `kms_key_version`, and the newest `job_runs.created_at` (how fresh the backup is).
4. Deletes the drill instance, and prints the time of each phase and whether gate 6 was met.

Any failure deletes the drill instance on the way out; `--keep` leaves it for a look and prints the
delete command. Drill instance cost: the tier's hourly price for the run (well under a dollar).
Run it from the repo root of a full checkout, so the revision check sees `apps/api/alembic`. The
first prod run needs the owner's go-ahead (it reads prod passwords and makes a prod-sized
instance for about 15 minutes).

## Cross-region backups

Prepared and OFF until the client decides (V1 hardening #4). Today `SQL_BACKUP_LOCATION=asia-south1`,
the same region as the zonal primary, so a regional outage takes both. The other accepted value is
`asia-south2` (Delhi), which keeps the data in India; any other value is refused before any gcloud
call. To switch on:
1. Set `SQL_BACKUP_LOCATION=asia-south2` in `prod.env` and commit.
2. `scripts/deploy/gcp/deploy.sh prod setup --dry-run`: on the live instance it prints one
   `gcloud sql instances patch aimos-db --backup-location=asia-south2` and changes nothing else.
3. `scripts/deploy/gcp/deploy.sh prod setup`. New backups go to asia-south2; backups already
   taken stay in asia-south1 until they age out (7 kept).
4. After the next nightly backup (20:30 UTC), run `restore-drill`: it restores from the new
   location into asia-south1.
To switch back, set `asia-south1` and rerun `setup`.

## Cost notes (list prices before GST; ADR-0001, EXECUTION-PLAN §5.4)

- **Staging:** Cloud SQL `db-custom-1-3840` (EXECUTION-PLAN decision 6: prod-like, not ADR-0001's
  shared core), about $60 a month always on or $25–35 on a stop schedule. Add the worker pool (one
  instance) and an api that scales to zero. Stop the DB off-hours with
  `gcloud sql instances patch aimos-db --activation-policy=NEVER --project=aimos-staging` (and
  `ALWAYS` in the morning).
- **Prod:** about $170 a month with the worker at 1 vCPU / 2 GiB (ADR-0001). Since V1 hardening #9
  the worker is 2 vCPU / 4 GiB (one always-on instance), which doubles the worker pool's line
  ("Worker capacity" below).
- **Prod edge (DPP-2):** about $40 a month for the load balancer, Cloud Armor Standard and the
  security log bucket (`docs/runbooks/EDGE-CUTOVER.md`).
- Cloud Build bills per build minute. Artifact Registry keeps 20 versions and drops old `sha-` tags
  after 30 days. The build-source bucket deletes after 7 days, and CSVs after 30.
- Budgets are not in this toolkit, because they need billing-account access: EXECUTION-PLAN §5.2
  step 4.

## Worker capacity (V1 hardening #9)

One worker instance runs every queue in one process: `default` 4 jobs at once (generation, bulk
import, image checks, brand embeds), `publish` 2, `periodic` 2 (`app/workers/settings.py`; code
constants, sized to the app pool of 10 connections per process). Image checks and derives run in
child processes under a per-process limit, `IMAGE_CHECK_CONCURRENCY`.

| Setting (`prod.env`) | Value | Why |
|---|---|---|
| `WORKER_CPU` | `2` | A second child only runs in parallel with a second vCPU. |
| `WORKER_MEMORY` | `4Gi` | Each child may map up to 1 GiB; the worker itself needs about 1 GiB. |
| `IMAGE_CHECK_CONCURRENCY` | `2` | About twice the photo throughput of one child: the per-brand photo cap of 20 live jobs drains about twice as fast when several brands upload at once. |
| `WORKER_INSTANCES` | `1` | Kept at one: a second instance doubles the bill again and also doubles the connection budget. |

`deploy.sh` refuses `IMAGE_CHECK_CONCURRENCY` outside 1-4, and above 1 it refuses a worker with
less than `IMAGE_CHECK_CONCURRENCY + 1` GiB (written as `<n>Gi`) or fewer than 2 vCPU. Staging keeps
1 vCPU / 2 GiB and one child.

When to raise it further: photo jobs stay `queued` for minutes while the worker's CPU is low (the
models are slow), or uploads keep hitting 429 at the per-brand cap. Then go to 3 children with
`WORKER_MEMORY=4Gi` and watch memory, or a second instance. When to lower it: the worker restarts
with an out-of-memory exit (`aimos-worker restarting` alert). Rollback: `WORKER_CPU=1`,
`WORKER_MEMORY=2Gi`, `IMAGE_CHECK_CONCURRENCY` blank, then `deploy`.

## Known limitations

- **The prod worker is on.** `prod.env` ships `WORKER_ENABLED=true` with a pinned `PIPELINE_REF`
  (the AI team's `mkos-pipeline`); a production worker still refuses `mkos-pipeline-stub` at boot,
  and `deploy.sh` refuses `WORKER_ENABLED=true` in production without `PIPELINE_REF`. A worker
  that is not running is an incident (`INCIDENTS.md`), not the expected state.
- **Ingress is Cloudflare in front of run.app** (`EDGE_PROVIDER=cloudflare`, ADR-0025,
  `EDGE-CLOUDFLARE.md`) at `https://api.kuinn.in`, with `ORIGIN_AUTH=enforce`. The load balancer
  and Cloud Armor path (`EDGE-CUTOVER.md`) waits for quota.
- **Alerts need `ALERT_EMAIL`.** It is blank in `prod.env` until the owner fills it in and runs
  `edge alerts` ("Alerts and Slack" above); until then only Sentry sees failures. A Slack copy of
  the Cloud Monitoring alerts also needs `ALERT_SLACK_CHANNEL` and the one-time console step.
- **Worker pool on gcloud 587.** Its bundled Python cannot load `run worker-pools deploy` (no
  `grpc`), so `deploy` detects that and applies the same settings with
  `run worker-pools replace <yaml>`. A Cloud Run service is not a fallback: the worker binds no
  port (`docker-entrypoint.sh`), so a service's startup probe would fail.
- **Public image copies (SI1, ADR-0026).** Marketplace image links are stable public URLs on an
  EXIF-free copy, served from a public bucket. The toolkit only passes `GCS_BUCKET_IMAGES_PUBLIC`
  and `IMAGE_PUBLIC_BASE_URL` through to the api, the worker and the maintenance job when both
  are set; it does not create the bucket, its IAM or the `images.kuinn.in` DNS record (the
  owner's step). Until both are set, a publish that would send images fails closed with
  `INTERNAL_ERROR` "image hosting is not configured". The owner's IAM, exactly (ADR-0026 §7):
  - the storage identity:
    `gcloud storage buckets add-iam-policy-binding gs://<public-bucket> --member=serviceAccount:<aimos-storage email> --role=roles/storage.objectUser`;
  - public read:
    `gcloud storage buckets add-iam-policy-binding gs://<public-bucket> --member=allUsers --role=roles/storage.legacyObjectReader`,
    with uniform bucket-level access on and public-access prevention off. Never grant
    `roles/storage.objectViewer` to `allUsers`: it carries `storage.objects.list`, so anyone could
    list every tenant's public names;
  - the check: `curl -s -o /dev/null -w '%{http_code}' https://storage.googleapis.com/storage/v1/b/<public-bucket>/o`
    must print 401 or 403, and an anonymous GET of a known published object must print 200.
- **A by-hand tenant purge needs the public copies gone first.** Run
  `python -m app.cli images unpublish --tenant <tenant_id>` (migrator) before
  `SELECT purge_tenant(...)`: the 0015 delete guard refuses the purge while any image still has a
  public name. `seed --reset` runs the same step itself.
- **Rotation.** A generated secret is generated only once. To rotate one, add a version
  (`openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add NAME --data-file=-`) and
  redeploy. For a DB role, also rerun `db-init`, then disable and rebuild its URL secret as in the
  restore steps.

## S4 import list (what `setup` creates)

APIs; the `_Default` log bucket retention; the Artifact Registry repo `aimos`; the service accounts
`aimos-{api,worker,migrate,ops,storage,build}` and their bindings; the key ring `marketplace-os` and
key `oauth-token-key`; the buckets `aimos-{images,csv,submissions}-<env>` and
`aimos-<env>-build-src`; the HMAC key; the Cloud SQL instance `aimos-db` and database
`marketplace_os`; every secret named above; the Cloud Run service `aimos-api`, the worker pool
`aimos-worker`, and the jobs `aimos-db-init`, `aimos-migrate`, `aimos-ops` and `aimos-ptd`.
