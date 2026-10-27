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
| `setup` | One time, rerunnable. Enables the APIs and sets log retention. Creates the Artifact Registry repo (immutable tags, cleanup policy), the service accounts, the KMS key ring `marketplace-os` with the keys `oauth-token-key` and `mfa-secret-key` (90-day rotation each; only the api service account may use `mfa-secret-key`, ADR-0021), the buckets (uniform access, public access prevention, CSV lifecycle, CORS for the dashboard origins) and an HMAC key into `S3_ACCESS_KEY`/`S3_SECRET_KEY`. Creates Cloud SQL and the database, the generated secrets and the per-secret IAM. Stops if `SENTRY_DSN` (or, with the worker on, `OPENAI_API_KEY`) has no value, and prints the command to add it. |
| `build` | Cloud Build of `apps/api/Dockerfile` from a `git archive` of a clean `HEAD`, pushed as `.../aimos/backend:sha-<12>`. Refuses uncommitted changes. Skips a tag that already exists. |
| `db-init` | One time, rerunnable. A one-off job of the same image (`APP_ROLE=shell`) runs `scripts/db/run_bootstrap.py` as `postgres` over the `/cloudsql` socket. It runs `0000_bootstrap.sql`, then sets the `app_rw`, `app_ro` and `migrator` passwords from SCRAM verifiers, under the `0000_role_passwords.sql` safety checks. |
| `migrate` | The migrate job (`APP_ROLE=migrate`, all as `migrator`): `alembic upgrade head`, the dictionary sync, `rules load` (the platform rules), then `templates load` (the Amazon and Flipkart mappings), each from the files in the image and each idempotent, so a rerun writes nothing. Rules load before templates, so a failed run never leaves a category generatable without its rules. The error map is not loaded: it stays empty until observed rows exist. A rule taken out of `rules.seed.json` stays stored and enabled: to retire one, keep it in the file with `"enabled": false`. Once the rules are loaded, validation, review, approval and the image gate evaluate them for every tenant, so results change for existing listings. It runs with `max-retries 0`, waits, and fails when the run fails. |
| `deploy` | The api service: public, the app does its own auth, Cloud SQL attached, secrets as env vars. A new revision gets 0% traffic until its tagged URL passes `smoke`, then takes all traffic. On the first deploy, `JWT_ISSUER` and the OAuth redirect URIs are re-pointed to the URL the service got. Then the worker pool (`WORKER_ENABLED=true` only), then `smoke`. |
| `release` | `build`, `migrate`, `deploy`: the normal update. |
| `smoke` | `GET /healthz` must answer 200 (on a `*.run.app` URL, whose front end answers `/healthz` itself with a 404, `GET /api/v1/auth/me` must answer 401 instead); `GET /readyz` must answer 200 with `"postgres":"ok"`. Retried for cold starts. |
| `tenant` | `tenant create --name N --slug S --owner-email E --staff-email F [--trial-days D]`, `tenant add-member --slug S --email E --role owner\|operator\|reviewer\|admin --staff-email F`, `tenant set-password --email E --staff-email F`, `tenant make-admin --email E --staff-email F`, and (DPP-1, after the identity check in `MFA-STAFF.md`) `tenant unlock --email E --staff-email F` (no password) and `tenant reset-mfa --email E --staff-email F`. Runs the staff CLI (`python -m app.cli`) in a one-off job. A new user's password is generated into `INITIAL_PASSWORD_<email>_<hash>` (8 groups of an upper-case letter, a lower-case letter and a digit, so it meets the DPP rules; must-change at the next sign-in), and the job reads it on stdin from a mounted file. The script prints the `gcloud secrets versions access` command that shows it, never the value. A rerun of `create` with a taken slug fails and changes nothing. |
| `status` | The service URL, latest revisions, worker pool, recent job runs and secret names (never values). |

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
- Optional: `API_PUBLIC_URL`, once a domain fronts the API. `JWT_ISSUER` and the OAuth redirect URIs
  follow it, and the Amazon/Flipkart applications must list the same callback strings.
- Everything else is generated: `JWT_SECRET`, `ADMIN_SESSION_SECRET`, the `postgres`, `app_rw`,
  `app_ro` and `migrator` passwords, `DATABASE_URL` (`app_rw` over
  `host=/cloudsql/<PROJECT>:<REGION>:<INSTANCE>`), `ALEMBIC_DATABASE_URL` (`migrator`), and the
  HMAC pair.

## Who reads what (least privilege)

| Account | Project roles | Resource roles |
|---|---|---|
| `aimos-api` | `cloudsql.client` | accessor on its 6 secrets; `cryptoKeyEncrypter` (encrypt only, ADR-0018) on the one key |
| `aimos-worker` | `cloudsql.client` | accessor on its 5 secrets (adds `OPENAI_API_KEY`); `cryptoKeyEncrypterDecrypter` on the key |
| `aimos-migrate` | `cloudsql.client` | accessor on `ALEMBIC_DATABASE_URL` |
| `aimos-ops` | `cloudsql.client` | accessor on the 4 DB passwords, `ALEMBIC_DATABASE_URL`, each `INITIAL_PASSWORD_*` |
| `aimos-storage` | none | `storage.objectAdmin` on the 3 app buckets: the identity behind the HMAC key |
| `aimos-build` | `logging.logWriter` | `artifactregistry.writer` on the repo; `objectViewer` on the build-source bucket |

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
- Cloud Build bills per build minute. Artifact Registry keeps 20 versions and drops old `sha-` tags
  after 30 days. The build-source bucket deletes after 7 days, and CSVs after 30.
- Budgets are not in this toolkit, because they need billing-account access: EXECUTION-PLAN §5.2
  step 4.

## Known limitations

- **Prod generation waits for the AI team's package.** `APP_ENV=production` with
  `APP_ROLE=worker` refuses `mkos-pipeline-stub` at boot (`app/core/config.py`), so `prod.env` ships
  `WORKER_ENABLED=false`. Turn it on once the image carries the real `mkos-pipeline`.
- **Marketplace tokens need the Cloud KMS wrapper.** Outside local, `app/core/crypto.py` raises
  `NotImplementedError` until the wrapper lands. The key and its IAM are ready, but connecting a
  store fails in staging and prod until then.
- **Ingress is the Cloud Run URL.** ADR-0001's load balancer (prod) and Firebase rewrite (staging)
  are not built. Set `API_PUBLIC_URL` when they are.
- **Worker pool on gcloud 587.** Its bundled Python cannot load `run worker-pools deploy` (no
  `grpc`), so `deploy` detects that and applies the same settings with
  `run worker-pools replace <yaml>`. A Cloud Run service is not a fallback: the worker binds no
  port (`docker-entrypoint.sh`), so a service's startup probe would fail.
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
`aimos-worker`, and the jobs `aimos-db-init`, `aimos-migrate` and `aimos-ops`.
