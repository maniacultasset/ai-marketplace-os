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
| `tenant` | `tenant create --name N --slug S --owner-email E --staff-email F [--trial-days D]`, `tenant add-member --slug S --email E --role owner\|operator\|reviewer\|admin --staff-email F`, `tenant set-password --email E --staff-email F`, `tenant make-admin --email E --staff-email F`, and (DPP-1, after the identity check in `MFA-STAFF.md`) `tenant unlock --email E --staff-email F` (no password) and `tenant reset-mfa --email E --staff-email F`. Runs the staff CLI (`python -m app.cli`) in a one-off job. A new user's password is generated into `INITIAL_PASSWORD_<email>_<hash>` (8 groups of an upper-case letter, a lower-case letter and a digit, so it meets the DPP rules; must-change at the next sign-in), and the job reads it on stdin from a mounted file. The script prints the `gcloud secrets versions access` command that shows it, never the value. A rerun of `create` with a taken slug fails and changes nothing. |
| `templates` | `templates refresh --product-type T --staff-email E --reason R` and `templates promote-definition --product-type T --version V [--digest D] --staff-email E --reason R` (DW2, `PTD-REFRESH.md`). Runs `python -m app.cli templates ...` in the one-off job `aimos-ptd` under its own identity `aimos-ptd` (secret access is per identity, so the shared ops identity never gets the token), with the Amazon app's public settings, `AMAZON_CLIENT_SECRET`, `AMAZON_PLATFORM_REFRESH_TOKEN` and the migrator URL. Only while `AMAZON_APP_ID` is set. The platform token is a self-authorization of the Kuinn app on the company's own seller account; the owner adds it with the printed `read -s` command. Only this job mounts it. |
| `status` | The service URL, latest revisions, worker pool, recent job runs and secret names (never values). |
| `edge` | Prod only (needs `EDGE_DOMAIN`; staging is refused). `edge setup` builds the global external load balancer for `api.kuinn.in` (managed certificate, SSL policy MODERN / TLS 1.2, serverless NEGs for the service and its `candidate` tag, logged backends with the Cloud Armor policy `kuinn-api-waf`); `edge rules` converges the policy to `armor-rules.yaml` rule by rule, every group in preview unless listed in `EDGE_ENFORCE`; `edge enforce` does the same after printing what leaves preview; `edge lock-ingress` / `edge unlock-ingress` close and reopen the `run.app` URLs; `edge alerts` sets up the uptime, worker and maintenance alerts (see "Alerts and Slack") plus the deny/429/5xx/Adaptive Protection alerts to `ALERT_EMAIL` and the 400-day `security-logs` bucket; `edge cors` resets the bucket CORS from `ALLOWED_ORIGINS`; `edge status` prints the read-only state and evidence. Order, gates and rollbacks: `docs/runbooks/EDGE-CUTOVER.md`. Interim (ADR-0025): with `EDGE_PROVIDER=cloudflare` only `edge alerts` (the provider-independent part, below), `edge cors` and `edge status` run, and the api gets the Worker's `EDGE_ORIGIN_SECRET`; see `docs/runbooks/EDGE-CLOUDFLARE.md`. |

`--sha <12 hex>` points `db-init`, `migrate`, `deploy`, `smoke` and `tenant` at an image other
than `HEAD`'s.

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
`EDGE_PROVIDER=cloudflare` it creates, once each (a rerun changes nothing, except the channel's
address when `ALERT_EMAIL` changed):

| What | Fires when |
|---|---|
| email channel `aimos-edge-alerts` | (to `ALERT_EMAIL`) |
| uptime check `aimos-edge-readyz` on `https://api.kuinn.in/readyz`, every 5 min; policy `aimos-edge uptime failing` | `/readyz` fails from 2 or more regions for 5 minutes (API or database down) |
| log metric `worker_alerts_checked`; policy `aimos-worker heartbeat missing` | no `periodic.alerts_checked` line from the worker pool for 15 minutes (worker down, hung or never started) |
| log metric `worker_started`; policy `aimos-worker restarting` | more than 2 `worker.started` lines in 30 minutes (a crash loop) |
| policy `aimos-maintenance errors` | any ERROR line of the `aimos-maintenance` job (a failed step or an incomplete DPP purge; at most one mail an hour) |

On the load balancer path (`EDGE_PROVIDER` blank) the same command adds the deny, 429, 5xx and
Adaptive Protection policies and the security log bucket. The two worker metrics filter on
`resource.type="cloud_run_worker_pool"`; at the first run, check in Logs Explorer that the
worker's lines carry that resource type, and fix the filter if Google names it otherwise.

**Slack.** `ALERT_SLACK_WEBHOOK` is an optional secret: `setup` creates it with no value and grants
it to the worker, prints how to add a value, and never stops for it. `deploy` binds it to the
worker only once it has an enabled version, so an empty secret never fails a deploy. The worker
posts the kinds in `SLACK_KINDS` (`app/core/alerts.py`): `llm_budget`, `llm_credit` (the OpenRouter
credit at 50/80/95 %), `llm_provider_refused` (AI paused), `queue_latency`,
`token_refresh_failed` and `job_dead_lettered` (each dead letter or failed publish, deduped per
job per day, at most 20 per job kind per 15 minutes, then one summary). To turn it on: make an
incoming webhook in the team's Slack, add it with the `read -rsp` command `setup` prints, then
`deploy.sh prod deploy` (or the next `release`). Every alert kind also reaches Sentry.

**Who owns what.** The owner of `ALERT_EMAIL` owns the Cloud Monitoring policies above. Sentry
alert rules (new issue, regression) are set in Sentry's UI by the on-call engineer named in
`docs/runbooks/INCIDENTS.md`; nothing in this toolkit creates them.

## Restore the database (point-in-time recovery)

PITR keeps `SQL_PITR_DAYS` (7) of logs. Restore into a new instance, never over the live one:
```bash
gcloud sql instances clone aimos-db aimos-db-restore --point-in-time=2026-10-11T08:00:00Z \
  --project=aimos-staging
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

## Cost notes (list prices before GST; ADR-0001, EXECUTION-PLAN §5.4)

- **Staging:** Cloud SQL `db-custom-1-3840` (EXECUTION-PLAN decision 6: prod-like, not ADR-0001's
  shared core), about $60 a month always on or $25–35 on a stop schedule. Add the worker pool (one
  instance) and an api that scales to zero. Stop the DB off-hours with
  `gcloud sql instances patch aimos-db --activation-policy=NEVER --project=aimos-staging` (and
  `ALWAYS` in the morning).
- **Prod:** about $170 a month with the worker (ADR-0001). Until the worker runs, it is Cloud SQL
  `db-custom-1-3840` plus one always-on api instance.
- **Prod edge (DPP-2):** about $40 a month for the load balancer, Cloud Armor Standard and the
  security log bucket (`docs/runbooks/EDGE-CUTOVER.md`).
- Cloud Build bills per build minute. Artifact Registry keeps 20 versions and drops old `sha-` tags
  after 30 days. The build-source bucket deletes after 7 days, and CSVs after 30.
- Budgets are not in this toolkit, because they need billing-account access: EXECUTION-PLAN §5.2
  step 4.

## Known limitations

- **Prod generation waits for the AI team's package.** `APP_ENV=production` with
  `APP_ROLE=worker` refuses `mkos-pipeline-stub` at boot (`app/core/config.py`), so `prod.env` ships
  `WORKER_ENABLED=false` and an empty `PIPELINE_REF`. Turn the worker on only after a build with
  `PIPELINE_REF` (see "The real pipeline").
- **Marketplace tokens need the Cloud KMS wrapper.** Outside local, `app/core/crypto.py` raises
  `NotImplementedError` until the wrapper lands. The key and its IAM are ready, but connecting a
  store fails in staging and prod until then.
- **Ingress is the Cloud Run URL.** ADR-0001's load balancer (prod) and Firebase rewrite (staging)
  are not built. Set `API_PUBLIC_URL` when they are.
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
