#!/usr/bin/env bash
# The by-hand GCP deploy toolkit (docs/runbooks/DEPLOY-GCP.md): one image, Cloud SQL, GCS, Secret
# Manager and Cloud KMS in asia-south1 (ADR-0001), run by a person in their own terminal.
#
#   scripts/deploy/gcp/deploy.sh <staging|prod> <subcommand> [--dry-run] [--sha SHA] [args]
#
#   preflight   gcloud, account, project, billing, APIs
#   setup       APIs, Artifact Registry, service accounts, KMS, buckets, HMAC key, Cloud SQL,
#               database, generated secrets; stops if SENTRY_DSN or OPENAI_API_KEY is missing
#   build       Cloud Build of apps/api/Dockerfile from a clean HEAD, tagged sha-<12>
#   db-init     0000_bootstrap.sql and the role passwords, as postgres, in a one-off job
#   migrate     the migrate job (alembic upgrade head, dictionary sync, rules load, templates load), waited on
#   deploy      the api service and the worker pool, then smoke
#   release     build, migrate, deploy
#   smoke       GET /healthz and /readyz on the service URL
#   tenant      create | add-member | set-password | make-admin | unlock | reset-mfa (staff CLI job)
#   status      service URL, revisions, job runs, secret names
#   edge        setup | rules | enforce | lock-ingress | unlock-ingress | alerts | cors | status (prod; needs EDGE_DOMAIN)
#
# Every subcommand describes before it creates, so a rerun is safe. Each gcloud command is printed
# before it runs; --dry-run prints them and runs none (its only gcloud call is a local `--help`
# probe for the worker-pool command, which never contacts Google Cloud). No secret value is
# printed or passed as an argument: generated values go from openssl straight into
# `gcloud secrets versions add --data-file=-`, the postgres password reaches Cloud SQL through a
# 0600 temp flags file removed at once, and jobs read the secrets Cloud Run mounts or injects.
# Bash 3.2 compatible (macOS).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
PLACEHOLDER="__FILL_ME__"
USER_SECRETS="SENTRY_DSN OPENAI_API_KEY GOOGLE_OAUTH_CLIENT_SECRET"
REQUIRED_APIS="run.googleapis.com sqladmin.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com cloudkms.googleapis.com storage.googleapis.com iam.googleapis.com logging.googleapis.com cloudresourcemanager.googleapis.com serviceusage.googleapis.com cloudscheduler.googleapis.com"
API_SECRETS="DATABASE_URL JWT_SECRET ADMIN_SESSION_SECRET S3_ACCESS_KEY S3_SECRET_KEY SENTRY_DSN"
WORKER_SECRETS="DATABASE_URL S3_ACCESS_KEY S3_SECRET_KEY SENTRY_DSN OPENAI_API_KEY"
MIGRATE_SECRETS="ALEMBIC_DATABASE_URL"
DB_INIT_SECRETS="POSTGRES_PASSWORD APP_RW_PASSWORD APP_RO_PASSWORD MIGRATOR_PASSWORD"
OPS_SECRETS="ALEMBIC_DATABASE_URL"
# The daily maintenance job (DPP 1.7): app_rw for retention, migrator for the account purge, the
# HMAC pair for the submission and image object deletes.
MAINT_SECRETS="DATABASE_URL ALEMBIC_DATABASE_URL S3_ACCESS_KEY S3_SECRET_KEY"
PW_MOUNT="/secrets/initial-password/value"

usage() {
  sed -n '5,18p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

say() { printf 'deploy: %s\n' "$*"; }
die() {
  printf 'deploy: %s\n' "$*" >&2
  exit 1
}

# ---- arguments and the env file ----
[ $# -ge 2 ] || usage
ENV_NAME="$1"
CMD="$2"
shift 2
case "$ENV_NAME" in staging | prod) ;; *) usage ;; esac
DRY_RUN=0
SHA=""
REST=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --sha) [ $# -ge 2 ] || usage; SHA="$2"; shift 2 ;;
    *) REST+=("$1"); shift ;;
  esac
done
# shellcheck disable=SC1090
. "$HERE/$ENV_NAME.env"
# The DPP-2 edge keys (docs/runbooks/EDGE-CUTOVER.md); an env file older than DPP-2 has none.
EDGE_DOMAIN="${EDGE_DOMAIN-}"
INGRESS="${INGRESS-all}"
EDGE_ENFORCE="${EDGE_ENFORCE-}"
JWT_ISSUER="${JWT_ISSUER-}"
ALERT_EMAIL="${ALERT_EMAIL-}"
# Only an environment with an edge domain gets the load balancer and monitoring APIs, so staging
# never gets an auto-created default VPC.
EDGE_APIS="compute.googleapis.com monitoring.googleapis.com"
[ -z "$EDGE_DOMAIN" ] || REQUIRED_APIS="$REQUIRED_APIS $EDGE_APIS"
# Process env, not env-file keys: how often and how far apart a smoke retries.
SMOKE_TRIES="${SMOKE_TRIES:-10}"
SMOKE_SLEEP="${SMOKE_SLEEP:-6}"

# ---- DPP-1 (ADR-0021): the MFA switch, refused before any gcloud call ----
# `MFA_ENFORCEMENT=all` with no grace date would lock every user out at once, and a value set by
# hand on Cloud Run is wiped by the next deploy (`--env-vars-file` replaces every env var), so the
# switch lives only in the env file and is checked here for every subcommand.
MFA_ENFORCEMENT="${MFA_ENFORCEMENT:-off}"
MFA_GRACE_UNTIL="${MFA_GRACE_UNTIL:-}"
KMS_MFA_KEY="${KMS_MFA_KEY:-mfa-secret-key}"
# DPP 1.7: the daily maintenance job (retention and the account purge, app.workers.maintenance),
# its runtime and scheduler identities and its Cloud Scheduler trigger. An env file older than
# these keys gets the defaults.
MAINT_JOB="${MAINT_JOB:-aimos-maintenance}"
SA_MAINT="${SA_MAINT:-aimos-maint}"
SA_SCHED="${SA_SCHED:-aimos-sched}"
MAINT_SCHEDULE="${MAINT_SCHEDULE:-0 3 * * *}"
MAINT_TIME_ZONE="${MAINT_TIME_ZONE:-Asia/Kolkata}"
# DPP 2.7.4: where Cloud SQL keeps its automated backups; an env file without the key keeps them in
# the instance's region, as before.
SQL_BACKUP_LOCATION="${SQL_BACKUP_LOCATION:-$REGION}"
case "$MFA_ENFORCEMENT" in
  off | all) ;;
  *) die "MFA_ENFORCEMENT must be off or all (in $ENV_NAME.env)" ;;
esac
if [ "$MFA_ENFORCEMENT" = "all" ] && [ -z "$MFA_GRACE_UNTIL" ]; then
  die "MFA_ENFORCEMENT=all needs MFA_GRACE_UNTIL (a past date means no grace)"
fi
if [ -n "$MFA_GRACE_UNTIL" ]; then
  python3 -c 'import datetime as d, sys; sys.exit(0 if d.datetime.fromisoformat(sys.argv[1]).utcoffset() is not None else 1)' \
    "$MFA_GRACE_UNTIL" 2>/dev/null ||
    die "MFA_GRACE_UNTIL must be ISO 8601 with an offset, e.g. 2026-11-20T00:00:00+05:30"
fi

# ---- ADR-0020: the Google sign-in switch, refused before any gcloud call ----
# Off by default. Its client secret is bound to the api (and required by `setup`) only while the
# switch is on, so a hotfix with the switch off never needs the secret. Switch it only in the env
# file plus a same-image redeploy (docs/runbooks/GOOGLE-SIGNIN.md).
GOOGLE_OAUTH_ENABLED="${GOOGLE_OAUTH_ENABLED:-false}"
GOOGLE_OAUTH_CLIENT_ID="${GOOGLE_OAUTH_CLIENT_ID:-}"
case "$GOOGLE_OAUTH_ENABLED" in
  true | false) ;;
  *) die "GOOGLE_OAUTH_ENABLED must be true or false (in $ENV_NAME.env)" ;;
esac
if [ "$GOOGLE_OAUTH_ENABLED" = "true" ]; then
  case "$GOOGLE_OAUTH_CLIENT_ID" in
    ?*.apps.googleusercontent.com) ;;
    *) die "GOOGLE_OAUTH_ENABLED=true needs GOOGLE_OAUTH_CLIENT_ID (<id>.apps.googleusercontent.com, in $ENV_NAME.env)" ;;
  esac
  API_SECRETS="$API_SECRETS GOOGLE_OAUTH_CLIENT_SECRET"
fi

LABELS="app=aimos,env=$APP_ENV"
CONN="$PROJECT:$REGION:$SQL_INSTANCE"
AR_HOST="$REGION-docker.pkg.dev"
IMAGE="$AR_HOST/$PROJECT/$AR_REPO/$IMAGE_NAME"
KMS_KEY_URI="projects/$PROJECT/locations/$REGION/keyRings/$KMS_KEYRING/cryptoKeys/$KMS_KEY"
# DPP-1 (ADR-0021): the MFA secrets' own key, for the api service only.
MFA_KMS_KEY_URI="projects/$PROJECT/locations/$REGION/keyRings/$KMS_KEYRING/cryptoKeys/$KMS_MFA_KEY"
S3_ENDPOINT_URL="https://storage.googleapis.com"
if [ "$AMAZON_USE_SANDBOX" = "true" ]; then
  AMAZON_SPAPI_ENDPOINT="https://sandbox.sellingpartnerapi-eu.amazon.com"
else
  AMAZON_SPAPI_ENDPOINT="https://sellingpartnerapi-eu.amazon.com"
fi
sa_email() { printf '%s@%s.iam.gserviceaccount.com' "$1" "$PROJECT"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/aimos-deploy.XXXXXX")"
chmod 0700 "$WORK"
# On every exit (success, die, error, Ctrl-C): a locked deploy's `candidate` tag is removed
# (candidate_close, ADR-0022), then the work dir.
CANDIDATE_ARMED=0
on_exit() {
  local rc=$?
  set +e
  [ "$CANDIDATE_ARMED" -eq 0 ] || candidate_close
  rm -rf "$WORK"
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ---- printing and running ----
show() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
}

# A mutating gcloud call: printed, then run unless --dry-run.
gc() {
  show gcloud "$@" --project="$PROJECT" --quiet
  [ "$DRY_RUN" -eq 1 ] || gcloud "$@" --project="$PROJECT" --quiet
}

# A read-only gcloud query: printed; its stdout is the result. Under --dry-run it runs nothing and
# fails, so every "exists?" check reads as "absent" and the create commands print.
gq() {
  show gcloud "$@" --project="$PROJECT" >&2
  [ "$DRY_RUN" -eq 0 ] || return 1
  gcloud "$@" --project="$PROJECT" --quiet 2>/dev/null
}

# A pipeline that carries a secret value: only its description is printed, never the value.
pipe_note() { printf '+ %s\n' "$*"; }

# IAM on a just-created service account can lag; a binding is retried a few times.
retry() {
  local n=1
  until "$@"; do
    [ "$DRY_RUN" -eq 0 ] && [ "$n" -lt 5 ] || return 1
    n=$((n + 1))
    say "retrying in 10 s (IAM propagation)"
    sleep 10
  done
}

project_number() {
  local number
  if number="$(gq projects describe "$PROJECT" --format='value(projectNumber)')"; then
    printf '%s' "$number"
  else
    printf '<PROJECT_NUMBER>'
  fi
}

# ---- secrets ----
secret_exists() { gq secrets describe "$1" --format='value(name)' >/dev/null; }

secret_has_version() {
  local found
  found="$(gq secrets versions list "$1" --filter='state:ENABLED' --limit=1 --format='value(name)')" || return 1
  [ -n "$found" ]
}

ensure_secret() {
  secret_exists "$1" ||
    gc secrets create "$1" --replication-policy=user-managed --locations="$REGION" --labels="$LABELS"
}

# NAME BYTES: a random hex value of BYTES bytes, generated only when the secret has none.
generate_secret() {
  ensure_secret "$1"
  if secret_has_version "$1"; then
    say "secret $1: kept (it has an enabled version)"
    return 0
  fi
  pipe_note "openssl rand -hex $2 | gcloud secrets versions add $1 --data-file=- --project=$PROJECT"
  [ "$DRY_RUN" -eq 1 ] && return 0
  openssl rand -hex "$2" | tr -d '\n' |
    gcloud secrets versions add "$1" --data-file=- --project="$PROJECT" --quiet
}

# NAME ROLE PASSWORD_SECRET: the psycopg URL over the Cloud SQL unix socket, from the role's
# password secret (ENV.md DATABASE_URL; host=/cloudsql/<PROJECT>:<REGION>:<INSTANCE>).
dsn_secret() {
  ensure_secret "$1"
  if secret_has_version "$1"; then
    say "secret $1: kept (it has an enabled version)"
    return 0
  fi
  pipe_note "printf 'postgresql+psycopg://$2:<$3>@/$DB_NAME?host=/cloudsql/$CONN' | gcloud secrets versions add $1 --data-file=- --project=$PROJECT"
  [ "$DRY_RUN" -eq 1 ] && return 0
  local password
  password="$(gcloud secrets versions access latest --secret="$3" --project="$PROJECT")"
  [ -n "$password" ] || die "$3 is empty"
  printf 'postgresql+psycopg://%s:%s@/%s?host=/cloudsql/%s' "$2" "$password" "$DB_NAME" "$CONN" |
    gcloud secrets versions add "$1" --data-file=- --project="$PROJECT" --quiet
  password=""
}

grant_secret() {
  retry gc secrets add-iam-policy-binding "$1" --member="serviceAccount:$(sa_email "$2")" \
    --role=roles/secretmanager.secretAccessor --format=none
}

# "ENV=SECRET:latest,..." for --set-secrets, from a space-separated list of names (each env var
# is named after its secret).
secret_refs() {
  local out="" name
  for name in $1; do out="${out:+$out,}$name=$name:latest"; done
  printf '%s' "$out"
}

# ---- preflight ----
check_filled() {
  local name value
  for name in ALLOWED_ORIGINS DASHBOARD_BASE_URL; do
    value="${!name}"
    case "$value" in
      "" | *"$PLACEHOLDER"*) die "$name is not filled in scripts/deploy/gcp/$ENV_NAME.env" ;;
      https://*) ;;
      *) die "$name must be https origins (scripts/deploy/gcp/$ENV_NAME.env)" ;;
    esac
  done
  case "$JWT_ISSUER" in
    "" | https://*) ;;
    *) die "JWT_ISSUER, when set, must be https (scripts/deploy/gcp/$ENV_NAME.env)" ;;
  esac
}

cmd_preflight() {
  command -v gcloud >/dev/null || die "gcloud is not installed"
  command -v openssl >/dev/null || die "openssl is not installed"
  command -v python3 >/dev/null || die "python3 is not installed"
  local account="" project="" billing="" enabled="" missing="" api
  show gcloud config get-value account
  [ "$DRY_RUN" -eq 1 ] || account="$(gcloud config get-value account 2>/dev/null || true)"
  show gcloud config get-value project
  [ "$DRY_RUN" -eq 1 ] || project="$(gcloud config get-value project 2>/dev/null || true)"
  show gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)'
  [ "$DRY_RUN" -eq 1 ] || billing="$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)')"
  show gcloud services list --enabled --project="$PROJECT" --format='value(config.name)'
  [ "$DRY_RUN" -eq 1 ] || enabled="$(gcloud services list --enabled --project="$PROJECT" --format='value(config.name)')"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "required APIs: $REQUIRED_APIS"
    return 0
  fi
  [ -n "$account" ] || die "no gcloud account: run gcloud auth login"
  [ "$project" = "$PROJECT" ] ||
    die "gcloud project is '$project', want $PROJECT: run gcloud config set project $PROJECT"
  [ "$billing" = "True" ] || die "billing is not enabled on $PROJECT"
  for api in $REQUIRED_APIS; do
    printf '%s\n' "$enabled" | grep -qx "$api" || missing="$missing $api"
  done
  say "account $account, project $PROJECT, billing on"
  if [ -n "$missing" ]; then
    say "APIs not enabled yet (setup enables them):$missing"
  else
    say "all required APIs are enabled"
  fi
}

# ---- setup ----
# The dashboard PUTs to and GETs from V4 signed URLs straight from the browser (S17b); `setup` and
# `edge cors` both set this.
bucket_cors() {
  local bucket
  ALLOWED_ORIGINS="$ALLOWED_ORIGINS" python3 -c '
import json, os
origins = [o.strip() for o in os.environ["ALLOWED_ORIGINS"].split(",") if o.strip()]
print(json.dumps([{"origin": origins, "method": ["GET", "HEAD", "PUT"],
                   "responseHeader": ["Content-Type", "If-None-Match"], "maxAgeSeconds": 3600}]))' >"$WORK/cors.json"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "cors $WORK/cors.json:"
    sed 's/^/    /' "$WORK/cors.json"
  fi
  for bucket in "$BUCKET_IMAGES" "$BUCKET_CSV"; do
    gc storage buckets update "gs://$bucket" --cors-file="$WORK/cors.json"
  done
}

setup_apis() {
  # shellcheck disable=SC2086
  gc services enable $REQUIRED_APIS
  gc logging buckets update _Default --location=global --retention-days="$LOG_RETENTION_DAYS"
}

# The 400-day security-logs bucket (DPP 2.6.1: security logs kept at least 12 months). Shared by
# `setup` (the api's own events) and `edge alerts` (the load balancer's).
ensure_security_bucket() {
  if gq logging buckets describe "$EDGE_LOG_BUCKET" --location=global --format='value(name)' >/dev/null; then
    gc logging buckets update "$EDGE_LOG_BUCKET" --location=global --retention-days=400
  else
    gc logging buckets create "$EDGE_LOG_BUCKET" --location=global --retention-days=400
  fi
}

# DPP 2.6.1: the api's security events outlive the 30-day _Default bucket. The sink copies the
# sign-in events (`auth.*` log lines: ids and an ip hash only, app/services/auth_events.py) and the
# refused requests (401, 403, 429) from Cloud Run's request log into security-logs. OAuth callback
# URLs carry code and state, so they are never copied.
setup_security_logs() {
  local filter dest
  ensure_security_bucket
  filter="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$API_SERVICE\" AND (jsonPayload.message=~\"^auth\\\\.\" OR (logName:\"run.googleapis.com%2Frequests\" AND (httpRequest.status=401 OR httpRequest.status=403 OR httpRequest.status=429) AND NOT httpRequest.requestUrl:\"/callback\"))"
  dest="logging.googleapis.com/projects/$PROJECT/locations/global/buckets/$EDGE_LOG_BUCKET"
  if gq logging sinks describe "$APP_SECURITY_SINK" --format='value(name)' >/dev/null; then
    gc logging sinks update "$APP_SECURITY_SINK" "$dest" --log-filter="$filter"
  else
    gc logging sinks create "$APP_SECURITY_SINK" "$dest" --log-filter="$filter"
  fi
}

setup_registry() {
  gq artifacts repositories describe "$AR_REPO" --location="$REGION" --format='value(name)' >/dev/null ||
    gc artifacts repositories create "$AR_REPO" --location="$REGION" --repository-format=docker \
      --immutable-tags --labels="$LABELS"
  cat >"$WORK/ar-cleanup.json" <<'JSON'
[{"name":"keep-recent","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":20}},
 {"name":"drop-untagged","action":{"type":"Delete"},"condition":{"tagState":"untagged","olderThan":"14d"}},
 {"name":"drop-old-sha","action":{"type":"Delete"},"condition":{"tagState":"tagged","tagPrefixes":["sha-"],"olderThan":"30d"}}]
JSON
  gc artifacts repositories set-cleanup-policies "$AR_REPO" --location="$REGION" \
    --policy="$WORK/ar-cleanup.json" --no-dry-run --format=none
}

setup_accounts() {
  local pair name
  for pair in "$SA_API:API runtime" "$SA_WORKER:worker runtime" "$SA_MIGRATE:migrate job" \
    "$SA_OPS:one-off staff jobs" "$SA_STORAGE:GCS HMAC identity" "$SA_BUILD:Cloud Build" \
    "$SA_MAINT:daily maintenance job" "$SA_SCHED:maintenance scheduler"; do
    name="${pair%%:*}"
    gq iam service-accounts describe "$(sa_email "$name")" --format='value(email)' >/dev/null ||
      gc iam service-accounts create "$name" --display-name="aimos ${pair#*:}"
  done
  for name in "$SA_API" "$SA_WORKER" "$SA_MIGRATE" "$SA_OPS" "$SA_MAINT"; do
    retry gc projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$(sa_email "$name")" \
      --role=roles/cloudsql.client --condition=None --format=none
  done
  retry gc projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$(sa_email "$SA_BUILD")" \
    --role=roles/logging.logWriter --condition=None --format=none
  retry gc artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" \
    --member="serviceAccount:$(sa_email "$SA_BUILD")" --role=roles/artifactregistry.writer --format=none
}

setup_kms() {
  gq kms keyrings describe "$KMS_KEYRING" --location="$REGION" --format='value(name)' >/dev/null ||
    gc kms keyrings create "$KMS_KEYRING" --location="$REGION"
  if ! gq kms keys describe "$KMS_KEY" --keyring="$KMS_KEYRING" --location="$REGION" \
    --format='value(name)' >/dev/null; then
    local next
    next="$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
    gc kms keys create "$KMS_KEY" --keyring="$KMS_KEYRING" --location="$REGION" \
      --purpose=encryption --rotation-period=90d --next-rotation-time="$next" --labels="$LABELS"
  fi
  # ADR-0018: the api only seals (encrypt); the worker seals and unseals (encrypt and decrypt).
  local pair
  for pair in "$SA_API:roles/cloudkms.cryptoKeyEncrypter" \
    "$SA_WORKER:roles/cloudkms.cryptoKeyEncrypterDecrypter"; do
    retry gc kms keys add-iam-policy-binding "$KMS_KEY" --keyring="$KMS_KEYRING" --location="$REGION" \
      --member="serviceAccount:$(sa_email "${pair%%:*}")" --role="${pair#*:}" --format=none
  done
  # DPP-1 (ADR-0021): the MFA secrets' own key. Only the api seals and opens them (encrypt and
  # decrypt on this key alone); the worker and the jobs get no binding.
  if ! gq kms keys describe "$KMS_MFA_KEY" --keyring="$KMS_KEYRING" --location="$REGION" \
    --format='value(name)' >/dev/null; then
    local next_mfa
    next_mfa="$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
    gc kms keys create "$KMS_MFA_KEY" --keyring="$KMS_KEYRING" --location="$REGION" \
      --purpose=encryption --rotation-period=90d --next-rotation-time="$next_mfa" --labels="$LABELS"
  fi
  retry gc kms keys add-iam-policy-binding "$KMS_MFA_KEY" --keyring="$KMS_KEYRING" --location="$REGION" \
    --member="serviceAccount:$(sa_email "$SA_API")" --role=roles/cloudkms.cryptoKeyEncrypterDecrypter \
    --format=none
}

ensure_bucket() {
  gq storage buckets describe "gs://$1" --format='value(name)' >/dev/null ||
    gc storage buckets create "gs://$1" --location="$REGION" --uniform-bucket-level-access \
      --public-access-prevention --default-storage-class=STANDARD
}

setup_buckets() {
  local bucket
  for bucket in "$BUCKET_IMAGES" "$BUCKET_CSV" "$BUCKET_SUBMISSIONS" "$BUILD_BUCKET"; do
    ensure_bucket "$bucket"
  done
  printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":%s}}]}\n' "$CSV_RETENTION_DAYS" >"$WORK/csv-lifecycle.json"
  gc storage buckets update "gs://$BUCKET_CSV" --lifecycle-file="$WORK/csv-lifecycle.json"
  printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":7}}]}\n' >"$WORK/build-lifecycle.json"
  gc storage buckets update "gs://$BUILD_BUCKET" --lifecycle-file="$WORK/build-lifecycle.json"
  bucket_cors
  for bucket in "$BUCKET_IMAGES" "$BUCKET_CSV" "$BUCKET_SUBMISSIONS"; do
    retry gc storage buckets add-iam-policy-binding "gs://$bucket" \
      --member="serviceAccount:$(sa_email "$SA_STORAGE")" --role=roles/storage.objectAdmin --format=none
  done
  retry gc storage buckets add-iam-policy-binding "gs://$BUILD_BUCKET" \
    --member="serviceAccount:$(sa_email "$SA_BUILD")" --role=roles/storage.objectViewer --format=none
}

# The app reaches GCS through the S3-compatible XML API (app/adapters/storage.py), so it signs
# with an HMAC key of the storage identity; the key's access id and secret go straight into
# S3_ACCESS_KEY / S3_SECRET_KEY. Only the storage identity holds bucket roles.
setup_hmac() {
  ensure_secret S3_ACCESS_KEY
  ensure_secret S3_SECRET_KEY
  local have=0
  ! secret_has_version S3_ACCESS_KEY || have=$((have + 1))
  ! secret_has_version S3_SECRET_KEY || have=$((have + 1))
  if [ "$have" -eq 2 ]; then
    say "secrets S3_ACCESS_KEY, S3_SECRET_KEY: kept"
    return 0
  fi
  # Half a pair means an earlier run stopped between the two adds: a new key would leave the
  # first one active and unused, so a person resolves it.
  [ "$have" -eq 0 ] || die "only one of S3_ACCESS_KEY / S3_SECRET_KEY has a version: deactivate \
and delete that HMAC key (gcloud storage hmac list --project=$PROJECT), disable the lone secret \
version, then rerun setup"
  pipe_note "gcloud storage hmac create $(sa_email "$SA_STORAGE") --format=json | (access id -> S3_ACCESS_KEY, secret -> S3_SECRET_KEY)"
  [ "$DRY_RUN" -eq 1 ] && return 0
  local key
  key="$(gcloud storage hmac create "$(sa_email "$SA_STORAGE")" --project="$PROJECT" --format=json)"
  printf '%s' "$key" | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["metadata"]["accessId"])' |
    gcloud secrets versions add S3_ACCESS_KEY --data-file=- --project="$PROJECT" --quiet
  printf '%s' "$key" | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["secret"])' |
    gcloud secrets versions add S3_SECRET_KEY --data-file=- --project="$PROJECT" --quiet
  key=""
}

# The flags 0000_role_passwords.sql needs before it sets a password (no statement or failing
# statement is logged or kept with its verifier), plus the ADR-0001 connection cap.
sql_flags() {
  local flags="log_min_error_statement=panic,pg_stat_statements.track_utility=off"
  [ -z "$SQL_MAX_CONNECTIONS" ] || flags="$flags,max_connections=$SQL_MAX_CONNECTIONS"
  printf '%s' "$flags"
}

wait_sql_runnable() {
  [ "$DRY_RUN" -eq 1 ] && return 0
  local state waited=0
  until state="$(gq sql instances describe "$SQL_INSTANCE" --format='value(state)')" && [ "$state" = "RUNNABLE" ]; do
    [ "$waited" -lt 2400 ] || die "Cloud SQL $SQL_INSTANCE is not RUNNABLE after 40 min (state '$state')"
    sleep 30
    waited=$((waited + 30))
  done
}

setup_sql() {
  local protection="--no-deletion-protection"
  [ "$SQL_DELETION_PROTECTION" != "true" ] || protection="--deletion-protection"
  if gq sql instances describe "$SQL_INSTANCE" --format='value(name)' >/dev/null; then
    local current want
    want="$(printf '%s' "$(sql_flags)" | tr ',' '\n' | sort | paste -sd, -)"
    current="$(gq sql instances describe "$SQL_INSTANCE" --format=json |
      python3 -c 'import json,sys; f=json.load(sys.stdin)["settings"].get("databaseFlags",[]); print(",".join(sorted(x["name"]+"="+x["value"] for x in f)))')"
    if [ "$current" != "$want" ]; then
      say "Cloud SQL flags differ ('$current'); patching to '$want' (this may restart the instance)"
      gc sql instances patch "$SQL_INSTANCE" --database-flags="$(sql_flags)"
    fi
    # DPP 2.7.4: new backups go to SQL_BACKUP_LOCATION; existing backups stay where they were taken.
    current="$(gq sql instances describe "$SQL_INSTANCE" --format='value(settings.backupConfiguration.location)')"
    if [ "$current" != "$SQL_BACKUP_LOCATION" ]; then
      say "Cloud SQL backups are in '$current'; moving new backups to $SQL_BACKUP_LOCATION"
      gc sql instances patch "$SQL_INSTANCE" --backup-location="$SQL_BACKUP_LOCATION"
    fi
  else
    gc sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_17 --edition=ENTERPRISE \
      --tier="$SQL_TIER" --region="$REGION" --availability-type=zonal --storage-type=SSD \
      --storage-size="${SQL_STORAGE_GB}GB" --storage-auto-increase \
      --backup-start-time=20:30 --backup-location="$SQL_BACKUP_LOCATION" --retained-backups-count=7 \
      --enable-point-in-time-recovery --retained-transaction-log-days="$SQL_PITR_DAYS" \
      --retain-backups-on-delete --maintenance-window-day=SUN --maintenance-window-hour=21 \
      --database-flags="$(sql_flags)" --insights-config-query-insights-enabled \
      --assign-ip --connector-enforcement=REQUIRED --ssl-mode=ENCRYPTED_ONLY \
      "$protection" --async
  fi
  wait_sql_runnable
  gq sql databases describe "$DB_NAME" --instance="$SQL_INSTANCE" --format='value(name)' >/dev/null ||
    gc sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"
}

# The postgres password goes to Cloud SQL through a 0600 flags file (never argv), then is removed.
sync_postgres_password() {
  pipe_note "gcloud sql users set-password postgres --instance=$SQL_INSTANCE --flags-file=<0600 temp file holding POSTGRES_PASSWORD> --project=$PROJECT"
  [ "$DRY_RUN" -eq 1 ] && return 0
  local flags="$WORK/postgres-password.yaml" password
  # an assignment, so a failed read stops the script instead of setting an empty password
  password="$(gcloud secrets versions access latest --secret=POSTGRES_PASSWORD --project="$PROJECT")"
  [ -n "$password" ] || die "POSTGRES_PASSWORD is empty"
  (
    umask 077
    printf -- '--password: "%s"\n' "$password" >"$flags"
  )
  password=""
  gcloud sql users set-password postgres --instance="$SQL_INSTANCE" --flags-file="$flags" \
    --project="$PROJECT" --quiet
  rm -f "$flags"
}

setup_secrets() {
  generate_secret JWT_SECRET 32
  generate_secret ADMIN_SESSION_SECRET 32
  generate_secret POSTGRES_PASSWORD 24
  generate_secret APP_RW_PASSWORD 24
  generate_secret APP_RO_PASSWORD 24
  generate_secret MIGRATOR_PASSWORD 24
  dsn_secret DATABASE_URL app_rw APP_RW_PASSWORD
  dsn_secret ALEMBIC_DATABASE_URL migrator MIGRATOR_PASSWORD
  local name
  for name in $USER_SECRETS; do ensure_secret "$name"; done
  for name in $API_SECRETS; do grant_secret "$name" "$SA_API"; done
  for name in $WORKER_SECRETS; do grant_secret "$name" "$SA_WORKER"; done
  for name in $MIGRATE_SECRETS; do grant_secret "$name" "$SA_MIGRATE"; done
  for name in $DB_INIT_SECRETS $OPS_SECRETS; do grant_secret "$name" "$SA_OPS"; done
  for name in $MAINT_SECRETS; do grant_secret "$name" "$SA_MAINT"; done
}

# The two values only a person can supply. Each missing one gets a command for the user's OWN
# terminal: `read -s` keeps the value off the screen, out of argv and out of shell history.
check_user_secrets() {
  local name missing=0 wanted="SENTRY_DSN"
  # Only the worker embeds (S16): a deployment without the worker pool needs no OpenAI key yet.
  [ "$WORKER_ENABLED" != "true" ] || wanted="$wanted OPENAI_API_KEY"
  # ADR-0020: the Google client secret, only while Google sign-in is switched on
  [ "$GOOGLE_OAUTH_ENABLED" != "true" ] || wanted="$wanted GOOGLE_OAUTH_CLIENT_SECRET"
  for name in $wanted; do
    if secret_has_version "$name"; then
      say "secret $name: present"
    else
      missing=1
      say "secret $name has no value. Add it in your own terminal (the value is never echoed):"
      printf "  bash -c 'read -rsp \"%s: \" v; echo; printf %%s \"\$v\" | gcloud secrets versions add %s --data-file=- --project=%s'\n" \
        "$name" "$name" "$PROJECT"
    fi
  done
  if [ "$missing" -eq 1 ]; then
    [ "$DRY_RUN" -eq 1 ] && return 0
    die "setup stops here: add the secrets above, then rerun: scripts/deploy/gcp/deploy.sh $ENV_NAME setup"
  fi
}

cmd_setup() {
  check_filled
  cmd_preflight
  setup_apis
  setup_security_logs
  setup_registry
  setup_accounts
  setup_kms
  setup_buckets
  setup_hmac
  setup_sql
  setup_secrets
  sync_postgres_password
  check_user_secrets
  if [ "$DRY_RUN" -eq 1 ]; then
    say "setup dry run for $PROJECT: nothing was changed"
  else
    say "setup done for $PROJECT"
  fi
}

# ---- build ----
git_sha() {
  if [ -n "$SHA" ]; then
    printf '%s' "$SHA"
  else
    git -C "$ROOT" rev-parse --short=12 HEAD
  fi
}

cmd_build() {
  [ -z "$SHA" ] || die "build always builds HEAD; drop --sha"
  if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
    die "the tree has uncommitted changes: commit or stash them (the image must be one commit)"
  fi
  local sha tag
  sha="$(git_sha)"
  tag="sha-$sha"
  if gq artifacts docker images describe "$IMAGE:$tag" --format='value(image_summary.digest)' >/dev/null; then
    say "image $IMAGE:$tag exists (tags are immutable); nothing to build"
    return 0
  fi
  show git -C "$ROOT" archive --format=tar.gz -o "$WORK/source.tgz" HEAD
  git -C "$ROOT" archive --format=tar.gz -o "$WORK/source.tgz" HEAD
  gc builds submit "$WORK/source.tgz" --config="$HERE/cloudbuild.yaml" --region="$REGION" \
    --service-account="projects/$PROJECT/serviceAccounts/$(sa_email "$SA_BUILD")" \
    --gcs-source-staging-dir="gs://$BUILD_BUCKET/source" \
    --substitutions="_IMAGE=$IMAGE:$tag,_GIT_SHA=$sha"
}

# IMAGE@sha256:... of the built sha (deploys go by digest, never by a movable tag).
image_ref() {
  local tag digest
  tag="sha-$(git_sha)"
  if digest="$(gq artifacts docker images describe "$IMAGE:$tag" --format='value(image_summary.digest)')"; then
    [ -n "$digest" ] || die "no digest for $IMAGE:$tag"
    printf '%s@%s' "$IMAGE" "$digest"
  elif [ "$DRY_RUN" -eq 1 ]; then
    printf '%s:%s' "$IMAGE" "$tag"
  else
    die "no image $IMAGE:$tag: run scripts/deploy/gcp/deploy.sh $ENV_NAME build (or pass --sha)"
  fi
}

# ---- jobs ----
# JOB: execute, wait, and fail unless the execution succeeded.
run_job() {
  show gcloud run jobs execute "$1" --region="$REGION" --wait --format='value(metadata.name)' --project="$PROJECT"
  [ "$DRY_RUN" -eq 1 ] && return 0
  local execution succeeded
  if ! execution="$(gcloud run jobs execute "$1" --region="$REGION" --wait \
    --format='value(metadata.name)' --project="$PROJECT" --quiet)"; then
    job_logs_hint "$1"
    die "job $1 failed"
  fi
  succeeded="$(gcloud run jobs executions describe "$execution" --region="$REGION" \
    --format='value(status.succeededCount)' --project="$PROJECT")"
  if [ "$succeeded" != "1" ]; then
    job_logs_hint "$1"
    die "job $1 execution $execution did not succeed"
  fi
  say "job $1: execution $execution succeeded"
}

job_logs_hint() {
  say "logs: gcloud logging read 'resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"$1\"' --project=$PROJECT --freshness=1h --limit=100 --format='value(textPayload)'"
}

cmd_db_init() {
  local image args mounts="" name dir
  image="$(image_ref)"
  args="/src/scripts/db/run_bootstrap.py,--host,/cloudsql/$CONN,--dbname,$DB_NAME,--user,postgres"
  args="$args,--password-file,/secrets/postgres/value"
  for name in app_rw app_ro migrator; do
    args="$args,--$(printf '%s' "$name" | tr '_' '-')-password-file,/secrets/$name/value"
  done
  for name in $DB_INIT_SECRETS; do
    dir="$(printf '%s' "${name%_PASSWORD}" | tr '[:upper:]' '[:lower:]')"
    mounts="${mounts:+$mounts,}/secrets/$dir/value=$name:latest"
  done
  gc run jobs deploy "$DB_INIT_JOB" --image="$image" --region="$REGION" \
    --service-account="$(sa_email "$SA_OPS")" --set-cloudsql-instances="$CONN" \
    --set-env-vars="APP_ROLE=shell" --set-secrets="$mounts" --args="$args" \
    --tasks=1 --max-retries=0 --task-timeout=600s --cpu=1 --memory=512Mi --labels="$LABELS"
  run_job "$DB_INIT_JOB"
}

cmd_migrate() {
  local image
  image="$(image_ref)"
  gc run jobs deploy "$MIGRATE_JOB" --image="$image" --region="$REGION" \
    --service-account="$(sa_email "$SA_MIGRATE")" --set-cloudsql-instances="$CONN" \
    --set-env-vars="APP_ENV=$APP_ENV,APP_ROLE=migrate,LOG_FORMAT=json,GIT_SHA=$(git_sha)" \
    --set-secrets="$(secret_refs "$MIGRATE_SECRETS")" \
    --tasks=1 --max-retries=0 --task-timeout=900s --cpu=1 --memory=1Gi --labels="$LABELS"
  run_job "$MIGRATE_JOB"
}

# ---- deploy ----
service_url() { gq run services describe "$API_SERVICE" --region="$REGION" --format='value(status.url)'; }

# The API's public origin: the domain when one is set, else the live service URL, else the
# deterministic Cloud Run URL a new service gets (https://<service>-<project number>.<region>.run.app).
api_url() {
  local url
  if [ -n "$API_PUBLIC_URL" ]; then
    printf '%s' "$API_PUBLIC_URL"
  elif url="$(service_url)" && [ -n "$url" ]; then
    printf '%s' "$url"
  else
    printf 'https://%s-%s.%s.run.app' "$API_SERVICE" "$(project_number)" "$REGION"
  fi
}

# ROLE URL: KEY<TAB>VALUE lines, the non-secret settings of a deployed api or worker (ENV.md).
app_env() {
  local role="$1" url="$2"
  printf '%s\t%s\n' \
    APP_ENV "$APP_ENV" APP_ROLE "$role" LOG_FORMAT json LOG_LEVEL INFO GIT_SHA "$(git_sha)" \
    GCP_PROJECT_ID "$PROJECT" GCP_REGION "$REGION" SECRET_MANAGER_ENABLED true \
    ALLOWED_ORIGINS "$ALLOWED_ORIGINS" DASHBOARD_BASE_URL "$DASHBOARD_BASE_URL" ADMIN_ENABLED false \
    STORAGE_BACKEND gcs S3_ENDPOINT_URL "$S3_ENDPOINT_URL" GCS_BUCKET_IMAGES "$BUCKET_IMAGES" \
    GCS_BUCKET_CSV "$BUCKET_CSV" GCS_BUCKET_SUBMISSIONS "$BUCKET_SUBMISSIONS" \
    KMS_KEY_URI "$KMS_KEY_URI" AMAZON_USE_SANDBOX "$AMAZON_USE_SANDBOX" \
    AMAZON_SPAPI_ENDPOINT "$AMAZON_SPAPI_ENDPOINT" \
    AMAZON_OAUTH_REDIRECT_URI "$url/api/v1/marketplaces/amazon/callback" \
    FLIPKART_ENABLED "$FLIPKART_ENABLED" \
    FLIPKART_OAUTH_REDIRECT_URI "$url/api/v1/marketplaces/flipkart/callback" \
    APP_NAME "marketplace-os-$role" OTEL_SERVICE_NAME "marketplace-os-$role"
  [ "$role" != "api" ] || printf 'JWT_ISSUER\t%s\n' "${JWT_ISSUER:-$url}"
  # DPP-1 (ADR-0021): the MFA switch and its key, on the api only (the worker refuses the key)
  [ "$role" != "api" ] || printf 'MFA_ENFORCEMENT\t%s\nMFA_GRACE_UNTIL\t%s\nMFA_KMS_KEY_URI\t%s\n' \
    "$MFA_ENFORCEMENT" "$MFA_GRACE_UNTIL" "$MFA_KMS_KEY_URI"
  # ADR-0020: the Google sign-in switch and its public client id, on the api only (the redirect
  # URI is derived from DASHBOARD_BASE_URL by the api itself)
  [ "$role" != "api" ] || printf 'GOOGLE_OAUTH_ENABLED\t%s\nGOOGLE_OAUTH_CLIENT_ID\t%s\n' \
    "$GOOGLE_OAUTH_ENABLED" "$GOOGLE_OAUTH_CLIENT_ID"
}

yaml_quote() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"; }

# ROLE URL FILE: the --env-vars-file YAML (KEY: "value"; it replaces every env var).
env_file() {
  local key value
  : >"$3"
  while IFS=$'\t' read -r key value; do
    printf '%s: %s\n' "$key" "$(yaml_quote "$value")" >>"$3"
  done <<EOF
$(app_env "$1" "$2")
EOF
  if [ "$DRY_RUN" -eq 1 ]; then
    say "env file $3:"
    sed 's/^/    /' "$3"
  fi
}

# TAG: the tagged revision URL of the api service.
tag_url() {
  gq run services describe "$API_SERVICE" --region="$REGION" --format=json |
    python3 -c 'import json,sys; t=sys.argv[1]; print(next(x["url"] for x in json.load(sys.stdin)["status"]["traffic"] if x.get("tag")==t))' "$1"
}

# Every `sha-*` revision tag is a public URL straight to that revision, which bypasses settings a
# later revision changed (an old revision answers with MFA off or without the lockout). Once the new
# revision takes all traffic, every such tag goes, its own included; a rollback goes by revision name.
drop_revision_tags() {
  local stale
  if [ "$DRY_RUN" -eq 1 ]; then
    show gcloud run services update-traffic "$API_SERVICE" --region="$REGION" --remove-tags="<every sha-* tag>" --project="$PROJECT" --quiet
    return 0
  fi
  stale="$(gq run services describe "$API_SERVICE" --region="$REGION" --format=json |
    python3 -c 'import json,sys; print(",".join(x["tag"] for x in json.load(sys.stdin)["status"].get("traffic", []) if x.get("tag", "").startswith("sha-")))')" ||
    die "could not list the revision tags of $API_SERVICE"
  [ -z "$stale" ] || gc run services update-traffic "$API_SERVICE" --region="$REGION" --remove-tags="$stale"
}

deploy_api() {
  local image="$1" url="$2" tag existed=0 now value
  local -a args
  tag="sha-$(git_sha)"
  env_file api "$url" "$WORK/api-env.yaml"
  if service_url >/dev/null; then existed=1; fi
  args=(run deploy "$API_SERVICE" --image="$image" --region="$REGION"
    --service-account="$(sa_email "$SA_API")" --allow-unauthenticated --ingress="$INGRESS" --port=8080
    --cpu="$API_CPU" --memory="$API_MEMORY" --min-instances="$API_MIN_INSTANCES"
    --max-instances="$API_MAX_INSTANCES" --concurrency="$API_CONCURRENCY" --cpu-boost
    --execution-environment=gen2 --set-cloudsql-instances="$CONN"
    --env-vars-file="$WORK/api-env.yaml" --set-secrets="$(secret_refs "$API_SECRETS")"
    --labels="$LABELS")
  if [ "$existed" -eq 1 ] && [ "$INGRESS" = "$EDGE_LOCKED" ]; then
    # ADR-0022: run.app is refused, so the zero-traffic revision carries the `candidate` tag and is
    # smoked through the load balancer's header route, which matches only the random value
    # `edge setup` wrote into the URL map. The tag exists only for this deploy: it is removed on
    # every exit (on_exit), so between deploys the candidate backend has no target.
    value="$(candidate_value)" || exit 1
    CANDIDATE_ARMED=1
    gc "${args[@]}" --no-traffic --tag=candidate
    if [ "$DRY_RUN" -eq 0 ] && ! smoke "https://$EDGE_DOMAIN" "$CANDIDATE_HEADER_NAME: $value"; then
      candidate_close
      die "revision candidate failed smoke; traffic stays on the previous revision"
    fi
    gc run services update-traffic "$API_SERVICE" --region="$REGION" --to-latest
    candidate_close
  elif [ "$existed" -eq 1 ]; then
    # ADR-0001: the new revision takes no traffic until its tagged URL passes the smoke test.
    gc "${args[@]}" --no-traffic --tag="$tag"
    if [ "$DRY_RUN" -eq 0 ]; then
      smoke "$(tag_url "$tag")" || die "revision $tag failed smoke; traffic stays on the previous revision"
    fi
    gc run services update-traffic "$API_SERVICE" --region="$REGION" --to-latest
    drop_revision_tags
  else
    gc "${args[@]}"
    # JWT_ISSUER and the OAuth redirect URIs must be the URL the service actually got.
    if [ "$DRY_RUN" -eq 0 ] && [ -z "$API_PUBLIC_URL" ]; then
      now="$(service_url || true)"
      if [ -n "$now" ] && [ "$now" != "$url" ]; then
        local issuer="JWT_ISSUER=$now|"
        # A pinned issuer stays as set; only the redirect URIs follow the service URL.
        [ -z "$JWT_ISSUER" ] || issuer=""
        say "service URL is $now (not $url): updating ${issuer:+JWT_ISSUER and }the redirect URIs"
        gc run services update "$API_SERVICE" --region="$REGION" \
          --update-env-vars="^|^${issuer}AMAZON_OAUTH_REDIRECT_URI=$now/api/v1/marketplaces/amazon/callback|FLIPKART_OAUTH_REDIRECT_URI=$now/api/v1/marketplaces/flipkart/callback"
      fi
    fi
  fi
}

# The Knative YAML for `gcloud run worker-pools replace`, used when this SDK cannot load
# `worker-pools deploy` (gcloud 587's bundled Python lacks grpc). Secret refs only, no values.
worker_yaml() {
  local image="$1" url="$2" file="$3" key value name
  {
    printf 'apiVersion: run.googleapis.com/v1\nkind: WorkerPool\nmetadata:\n'
    printf '  name: %s\n  labels:\n    cloud.googleapis.com/location: %s\n' "$WORKER_POOL" "$REGION"
    printf '    app: aimos\n    env: %s\n' "$APP_ENV"
    printf '  annotations:\n    run.googleapis.com/scalingMode: manual\n'
    printf '    run.googleapis.com/manualInstanceCount: "%s"\n' "$WORKER_INSTANCES"
    printf 'spec:\n  template:\n    metadata:\n      annotations:\n'
    printf '        run.googleapis.com/cloudsql-instances: %s\n' "$CONN"
    printf '    spec:\n      serviceAccountName: %s\n      containers:\n' "$(sa_email "$SA_WORKER")"
    printf '        - image: %s\n          resources:\n            limits:\n' "$image"
    printf '              cpu: "%s"\n              memory: %s\n          env:\n' "$WORKER_CPU" "$WORKER_MEMORY"
    while IFS=$'\t' read -r key value; do
      printf '            - name: %s\n              value: %s\n' "$key" "$(yaml_quote "$value")"
    done <<EOF
$(app_env worker "$url")
EOF
    for name in $WORKER_SECRETS; do
      printf '            - name: %s\n              valueFrom:\n                secretKeyRef:\n' "$name"
      printf '                  name: %s\n                  key: latest\n' "$name"
    done
  } >"$file"
}

deploy_worker() {
  local image="$1" url="$2"
  if [ "$WORKER_ENABLED" != "true" ]; then
    say "worker pool skipped (WORKER_ENABLED=$WORKER_ENABLED in $ENV_NAME.env)"
    return 0
  fi
  if ! gcloud run worker-pools deploy --help >/dev/null 2>&1; then
    say "this gcloud cannot load 'run worker-pools deploy'; using 'run worker-pools replace'"
    worker_yaml "$image" "$url" "$WORK/worker-pool.yaml"
    if [ "$DRY_RUN" -eq 1 ]; then
      say "worker pool spec $WORK/worker-pool.yaml:"
      sed 's/^/    /' "$WORK/worker-pool.yaml"
    fi
    gc run worker-pools replace "$WORK/worker-pool.yaml"
    return 0
  fi
  env_file worker "$url" "$WORK/worker-env.yaml"
  gc run worker-pools deploy "$WORKER_POOL" --image="$image" --region="$REGION" \
    --service-account="$(sa_email "$SA_WORKER")" --cpu="$WORKER_CPU" --memory="$WORKER_MEMORY" \
    --instances="$WORKER_INSTANCES" --set-cloudsql-instances="$CONN" \
    --env-vars-file="$WORK/worker-env.yaml" --set-secrets="$(secret_refs "$WORKER_SECRETS")" \
    --labels="$LABELS"
}

# ADR-0018 and ADR-0021: the api reads both keys at run time. A key that `setup` never created (a
# release that added a key, deployed without `setup`) would fail every MFA enrolment or token seal
# after traffic moved, so the deploy refuses first.
kms_guard() {
  local key
  if [ "$DRY_RUN" -eq 1 ]; then
    say "kms guard: skipped (dry run)"
    return 0
  fi
  for key in "$KMS_KEY" "$KMS_MFA_KEY"; do
    gq kms keys describe "$key" --keyring="$KMS_KEYRING" --location="$REGION" \
      --format='value(name)' >/dev/null ||
      die "KMS key $key is missing or unreadable in $PROJECT: run scripts/deploy/gcp/deploy.sh $ENV_NAME setup first"
  done
  say "kms guard: OK ($KMS_KEY, $KMS_MFA_KEY)"
}

# IMAGE URL: the daily maintenance job on this release's image, and its Cloud Scheduler trigger
# (DPP 1.7, 2.1). The job runs `python -m app.workers.maintenance` as SA_MAINT (Cloud SQL client and
# its four secrets only) under the shell role, so it needs no worker. SA_SCHED may only run this
# one job (roles/run.invoker on the job). The run is idempotent: a retry or a second run deletes
# nothing twice. `setup` enables the Cloud Scheduler API and creates both accounts.
deploy_maintenance() {
  local image="$1" url="$2" uri verb
  env_file shell "$url" "$WORK/maintenance-env.yaml"
  gc run jobs deploy "$MAINT_JOB" --image="$image" --region="$REGION" \
    --service-account="$(sa_email "$SA_MAINT")" --set-cloudsql-instances="$CONN" \
    --env-vars-file="$WORK/maintenance-env.yaml" --set-secrets="$(secret_refs "$MAINT_SECRETS")" \
    --args="-m,app.workers.maintenance" --tasks=1 --parallelism=1 --max-retries=1 \
    --task-timeout=1800s --cpu=1 --memory=1Gi --labels="$LABELS"
  retry gc run jobs add-iam-policy-binding "$MAINT_JOB" --region="$REGION" \
    --member="serviceAccount:$(sa_email "$SA_SCHED")" --role=roles/run.invoker --format=none
  uri="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/$MAINT_JOB:run"
  verb=create
  if gq scheduler jobs describe "$MAINT_JOB-daily" --location="$REGION" --format='value(name)' >/dev/null; then
    verb=update
  fi
  gc scheduler jobs "$verb" http "$MAINT_JOB-daily" --location="$REGION" \
    --schedule="$MAINT_SCHEDULE" --time-zone="$MAINT_TIME_ZONE" --uri="$uri" --http-method=POST \
    --oauth-service-account-email="$(sa_email "$SA_SCHED")"
}

# The release ships the maintenance job and its trigger after traffic has moved, so a project whose
# `setup` predates them (no aimos-maint or aimos-sched account, Cloud Scheduler off) would fail
# half-released. Checked before the build, next to kms_guard.
setup_guard() {
  local name enabled
  if [ "$DRY_RUN" -eq 1 ]; then
    say "setup guard: skipped (dry run)"
    return 0
  fi
  for name in "$SA_MAINT" "$SA_SCHED"; do
    gq iam service-accounts describe "$(sa_email "$name")" --format='value(email)' >/dev/null ||
      die "service account $name is missing or unreadable in $PROJECT: run scripts/deploy/gcp/deploy.sh $ENV_NAME setup first"
  done
  enabled="$(gq services list --enabled --filter=config.name=cloudscheduler.googleapis.com \
    --format='value(config.name)' || true)"
  [ "$enabled" = "cloudscheduler.googleapis.com" ] ||
    die "cloudscheduler.googleapis.com is not enabled in $PROJECT: run scripts/deploy/gcp/deploy.sh $ENV_NAME setup first"
  say "setup guard: OK ($SA_MAINT, $SA_SCHED, Cloud Scheduler)"
}

# ADR-0020: with Google sign-in switched on, its client secret must have a value before the api
# is deployed (Cloud Run refuses a secret reference with no version, mid-release).
google_guard() {
  if [ "$GOOGLE_OAUTH_ENABLED" != "true" ]; then
    say "google guard: sign-in with Google is off"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    say "google guard: skipped (dry run)"
    return 0
  fi
  secret_has_version GOOGLE_OAUTH_CLIENT_SECRET ||
    die "secret GOOGLE_OAUTH_CLIENT_SECRET has no value in $PROJECT: run scripts/deploy/gcp/deploy.sh $ENV_NAME setup first"
  say "google guard: OK (GOOGLE_OAUTH_CLIENT_SECRET)"
}

cmd_deploy() {
  check_filled
  check_ingress
  local image url
  url="$(api_url)"
  live_guard "$url"
  kms_guard
  setup_guard
  google_guard
  image="$(image_ref)"
  deploy_api "$image" "$url"
  [ "$DRY_RUN" -eq 1 ] || [ -n "$API_PUBLIC_URL" ] || url="$(service_url)"
  deploy_worker "$image" "$url"
  deploy_maintenance "$image" "$url"
  cmd_smoke
}

# ---- smoke ----
# HEADER ARGS...: curl with the optional extra header (no possibly empty array under set -u).
smoke_curl() {
  local header="$1"
  shift
  if [ -n "$header" ]; then
    curl -sS --max-time 30 -H "$header" "$@"
  else
    curl -sS --max-time 30 "$@"
  fi
}

# URL [HEADER]: /healthz 200, then /readyz 200 with postgres ok (retried: staging scales to zero).
smoke() {
  local url="$1" header="${2:-}" code body n=0 live="/healthz" want="200" host shown
  host="${url#*://}"
  host="${host%%/*}"
  # A *.run.app front end answers /healthz itself with a 404 that never reaches the container,
  # and the edge's Armor path rule denies it, so there liveness is an API route that must answer
  # 401 without a token.
  case "$url" in *.run.app) live="/api/v1/auth/me" want="401" ;; esac
  if [ -n "$EDGE_DOMAIN" ] && [ "$host" = "$EDGE_DOMAIN" ]; then
    live="/api/v1/auth/me" want="401"
  fi
  if [ -n "$header" ]; then
    # The candidate route value is a credential for the unsmoked revision: never printed.
    shown="${header%%:*}: <redacted>"
    show curl -sS -o /dev/null -w '%{http_code}' -H "$shown" "$url$live"
    show curl -sS -H "$shown" "$url/readyz"
  else
    show curl -sS -o /dev/null -w '%{http_code}' "$url$live"
    show curl -sS "$url/readyz"
  fi
  [ "$DRY_RUN" -eq 0 ] || return 0
  until code="$(smoke_curl "$header" -o /dev/null -w '%{http_code}' "$url$live")" && [ "$code" = "$want" ]; do
    n=$((n + 1))
    [ "$n" -lt "$SMOKE_TRIES" ] || { say "smoke: $live answered '$code', want $want"; return 1; }
    sleep "$SMOKE_SLEEP"
  done
  n=0
  until body="$(smoke_curl "$header" "$url/readyz")" && case "$body" in *'"postgres":"ok"'*) true ;; *) false ;; esac; do
    n=$((n + 1))
    [ "$n" -lt "$SMOKE_TRIES" ] || { say "smoke: /readyz answered '$body'"; return 1; }
    sleep "$SMOKE_SLEEP"
  done
  say "smoke OK: $url ($live $want, /readyz postgres ok)"
}

cmd_smoke() {
  local url
  if [ -n "$API_PUBLIC_URL" ]; then
    url="$API_PUBLIC_URL"
  elif ! url="$(service_url)"; then
    [ "$DRY_RUN" -eq 1 ] || die "no service $API_SERVICE in $PROJECT"
    url="https://$API_SERVICE-<PROJECT_NUMBER>.$REGION.run.app"
  fi
  smoke "$url" || die "smoke failed for $url"
}

cmd_release() {
  check_ingress
  live_guard "$(api_url)"
  kms_guard
  setup_guard
  google_guard
  cmd_build
  cmd_migrate
  cmd_deploy
}

# ---- tenant (the staff CLI, app/cli.py, in a one-off job) ----
# An email -> its initial-password secret id (Secret Manager ids: letters, digits, - and _). The
# folded address is readable but not unique (a.b@x and a_b@x fold alike), so a hash of the exact
# lowercased address ends the id: two users never share an initial password.
password_secret() {
  local folded digest
  folded="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  digest="$(printf '%s' "$folded" | openssl dgst -sha256 -r | cut -c1-12)"
  printf 'INITIAL_PASSWORD_%s_%s' "$(printf '%s' "$folded" | tr -c 'a-z0-9' '_' | cut -c1-200)" "$digest"
}

# EMAIL: a generated initial password that meets the DPP 1.4.1 rules (DPP-1): 8 groups of an
# upper-case letter, a lower-case letter and a digit joined by '-' (31 characters, about 100 bits).
# No letter run is longer than 2, so it never holds a 3-letter name token; the whole email local
# part (digits and '-' count too, e.g. "ab1") is also a personal token, so a value that holds it
# is drawn again. The value goes straight into Secret Manager and is never printed.
gen_password() {
  python3 -c '
import secrets, string as s, sys, unicodedata
local = unicodedata.normalize("NFKC", sys.argv[1].rpartition("@")[0]).casefold()
while True:
    value = "-".join(secrets.choice(s.ascii_uppercase) + secrets.choice(s.ascii_lowercase) + secrets.choice(s.digits) for _ in range(8))
    if len(local) < 3 or local not in value.casefold():
        break
print(value, end="")
' "$1"
}

# EMAIL FRESH: the user's initial-password secret; FRESH=1 adds a new version even if one exists.
ensure_password() {
  local secret
  secret="$(password_secret "$1")"
  ensure_secret "$secret"
  if [ "$2" -eq 1 ] || ! secret_has_version "$secret"; then
    pipe_note "gen_password | gcloud secrets versions add $secret --data-file=- --project=$PROJECT"
    if [ "$DRY_RUN" -eq 0 ]; then
      gen_password "$1" |
        gcloud secrets versions add "$secret" --data-file=- --project="$PROJECT" --quiet
    fi
  fi
  grant_secret "$secret" "$SA_OPS"
}

# [--no-password] EMAIL NOTE CLI_ARGS...: run `python -m app.cli CLI_ARGS` with the user's password
# secret on stdin. The entrypoint's shell role runs `python "$@"`; stdin is the mounted secret
# file. `--no-password` (DPP-1: `user unlock`) mounts no password secret and reads no stdin.
ops_job() {
  local with_password=1 email note secret image args arg secrets
  if [ "$1" = "--no-password" ]; then
    with_password=0
    shift
  fi
  email="$1" note="$2"
  shift 2
  secret="$(password_secret "$email")"
  image="$(image_ref)"
  if [ "$with_password" -eq 1 ]; then
    # The password reaches the CLI as an in-memory stdin: a file on the secret volume can report
    # isatty(), and the CLI would then prompt twice and hit EOF on the repeat.
    args='-c|import io, runpy, sys; sys.stdin = io.StringIO(open(sys.argv[1], encoding="utf-8").read()); sys.argv = ["app.cli", *sys.argv[2:]]; runpy.run_module("app.cli", run_name="__main__", alter_sys=True)'
    args="$args|$PW_MOUNT"
    secrets="ALEMBIC_DATABASE_URL=ALEMBIC_DATABASE_URL:latest,$PW_MOUNT=$secret:latest"
  else
    args='-m|app.cli'
    secrets="ALEMBIC_DATABASE_URL=ALEMBIC_DATABASE_URL:latest"
  fi
  # gcloud refuses a repeated --args value, so `--flag value` pairs travel as `--flag=value`
  # (the same email as --owner-email and --staff-email would otherwise repeat).
  while [ $# -gt 0 ]; do
    arg="$1"
    shift
    case "$arg" in
      --*=*) ;;
      --*) if [ $# -gt 0 ] && case "$1" in --*) false ;; *) true ;; esac; then arg="$arg=$1"; shift; fi ;;
    esac
    case "$arg" in *"|"*) die "an argument may not contain '|'" ;; esac
    args="$args|$arg"
  done
  gc run jobs deploy "$OPS_JOB" --image="$image" --region="$REGION" \
    --service-account="$(sa_email "$SA_OPS")" --set-cloudsql-instances="$CONN" \
    --set-env-vars="APP_ENV=$APP_ENV,APP_ROLE=shell,LOG_FORMAT=json" \
    --set-secrets="$secrets" \
    --args="^|^$args" --tasks=1 --max-retries=0 --task-timeout=300s --cpu=1 --memory=1Gi \
    --labels="$LABELS"
  run_job "$OPS_JOB"
  if [ "$with_password" -eq 0 ]; then
    say "$note"
    return 0
  fi
  say "$note Read it once with:"
  printf '  gcloud secrets versions access latest --secret=%s --project=%s\n' "$secret" "$PROJECT"
}

flag_value() {
  # NAME ARGS...: the value after --NAME in ARGS, or empty.
  local name="$1"
  shift
  while [ $# -gt 0 ]; do
    if [ "$1" = "--$name" ] && [ $# -ge 2 ]; then
      printf '%s' "$2"
      return 0
    fi
    shift
  done
}

cmd_tenant() {
  [ ${#REST[@]} -ge 1 ] || die "usage: tenant create --name N --slug S --owner-email E --staff-email F [--trial-days D] | add-member --slug S --email E --role owner|operator|reviewer|admin --staff-email F | set-password --email E --staff-email F | make-admin --email E --staff-email F | unlock --email E --staff-email F | reset-mfa --email E --staff-email F"
  local action="${REST[0]}" email
  local -a args
  args=("${REST[@]}")
  args=("${args[@]:1}")
  [ ${#args[@]} -ge 1 ] || die "tenant $action needs its flags (see: deploy.sh $ENV_NAME tenant)"
  case "$action" in
    create)
      email="$(flag_value owner-email "${args[@]}")"
      [ -n "$email" ] || die "tenant create needs --owner-email"
      ensure_password "$email" 0
      ops_job "$email" "the owner's initial password (a NEW user only; an existing user keeps theirs)." \
        tenant create "${args[@]}"
      ;;
    add-member)
      email="$(flag_value email "${args[@]}")"
      [ -n "$email" ] || die "tenant add-member needs --email"
      ensure_password "$email" 0
      ops_job "$email" "the member's initial password (a NEW user only; an existing user keeps theirs)." \
        tenant add-member "${args[@]}"
      ;;
    set-password)
      email="$(flag_value email "${args[@]}")"
      [ -n "$email" ] || die "tenant set-password needs --email"
      ensure_password "$email" 1
      ops_job "$email" "the new password of $email (its sessions are revoked)." \
        user set-password "${args[@]}"
      ;;
    make-admin)
      # ADR-0019: flags an existing user (create a staff tenant with them as owner first, so they
      # can log in) as a platform admin who approves sign-up requests.
      email="$(flag_value email "${args[@]}")"
      [ -n "$email" ] || die "tenant make-admin needs --email"
      ensure_password "$email" 0
      ops_job "$email" "$email is now a platform admin; its password is unchanged. Its initial password, if this job made one:" \
        user make-admin "${args[@]}"
      ;;
    unlock)
      # DPP-1 (ADR-0021; docs/runbooks/MFA-STAFF.md: the identity check first): clears a locked
      # account. No password is generated or mounted.
      email="$(flag_value email "${args[@]}")"
      [ -n "$email" ] || die "tenant unlock needs --email"
      ops_job --no-password "$email" "$email is unlocked." user unlock "${args[@]}"
      ;;
    reset-mfa)
      # DPP-1: a lost authenticator, after the runbook's identity check.
      email="$(flag_value email "${args[@]}")"
      [ -n "$email" ] || die "tenant reset-mfa needs --email"
      ensure_password "$email" 1
      ops_job "$email" "the new must-change password of $email (its factor and recovery codes are deleted and its sessions revoked)." \
        user reset-mfa "${args[@]}"
      ;;
    *) die "unknown tenant action '$action' (create | add-member | set-password | make-admin | unlock | reset-mfa)" ;;
  esac
}

# ---- edge (DPP-2) ----
# The API edge (ADR-0022; docs/runbooks/EDGE-CUTOVER.md): a global external Application Load
# Balancer for EDGE_DOMAIN with a Google-managed certificate and an SSL policy MODERN / TLS 1.2,
# two serverless NEGs to the api service (the service, and its `candidate` tag), and the Cloud
# Armor policy whose rules come from armor-rules.yaml. Prod only: staging has no EDGE_DOMAIN.
EDGE_LOCKED="internal-and-cloud-load-balancing"
CANDIDATE_HEADER_NAME="x-aimos-candidate"
EDGE_POLICY="kuinn-api-waf"
EDGE_IP="aimos-api-edge-ip"
EDGE_NEG="aimos-api-edge-neg"
EDGE_CANDIDATE_NEG="aimos-api-edge-candidate-neg"
EDGE_BE="aimos-api-edge-be"
EDGE_CANDIDATE_BE="aimos-api-edge-candidate-be"
EDGE_MAP="aimos-api-edge-map"
EDGE_SSL="aimos-api-edge-ssl"
EDGE_CERT="aimos-api-edge-cert"
EDGE_PROXY="aimos-api-edge-proxy"
EDGE_RULE="aimos-api-edge-https"
EDGE_RULES="$HERE/armor-rules.yaml"
EDGE_UPTIME="aimos-edge-readyz"
EDGE_CHANNEL="aimos-edge-alerts"
EDGE_SINK="edge-security"
EDGE_LOG_BUCKET="security-logs"
APP_SECURITY_SINK="app-security"
EDGE_DENY_ALERT="${EDGE_DENY_ALERT:-50}"
EDGE_429_ALERT=100
EDGE_5XX_RATIO=0.05

# The helper armor_rules.py needs PyYAML: the repo's locked environment, or DEPLOY_PYTHON.
py() {
  if [ -n "${DEPLOY_PYTHON:-}" ]; then
    "$DEPLOY_PYTHON" "$@"
  else
    uv run --frozen --project "$ROOT" python "$@"
  fi
}

check_ingress() {
  case "$INGRESS" in
    all) ;;
    "$EDGE_LOCKED")
      [ -n "$EDGE_DOMAIN" ] ||
        die "INGRESS=$EDGE_LOCKED needs EDGE_DOMAIN (scripts/deploy/gcp/$ENV_NAME.env)"
      [ "$API_PUBLIC_URL" = "https://$EDGE_DOMAIN" ] ||
        die "locked ingress needs API_PUBLIC_URL=https://$EDGE_DOMAIN; run.app is refused"
      ;;
    *) die "INGRESS must be all or $EDGE_LOCKED, not '$INGRESS' (scripts/deploy/gcp/$ENV_NAME.env)" ;;
  esac
}

# The live api service as JSON; empty only when it does not exist (and on a dry run). Any other
# describe failure (auth, permission, network, quota) stops: the live-state guard and the lock's
# tag inventory must not read an unreadable service as "no service".
service_json() {
  local out
  show gcloud run services describe "$API_SERVICE" --region="$REGION" --format=json --project="$PROJECT" >&2
  [ "$DRY_RUN" -eq 0 ] || return 0
  if out="$(gcloud run services describe "$API_SERVICE" --region="$REGION" --format=json \
    --project="$PROJECT" --quiet 2>"$WORK/describe.err")"; then
    printf '%s' "$out"
    return 0
  fi
  if grep -qiE 'NOT_FOUND|Cannot find service' "$WORK/describe.err"; then
    return 0
  fi
  sed 's/^/    /' "$WORK/describe.err" >&2
  die "cannot read the live service $API_SERVICE (the error above is not NOT_FOUND); stopping"
}

# The candidate route's header value, read back from the live URL map: the 32-hex-digit random
# value `edge setup` wrote. Dies when the map is unreadable or predates the random value.
candidate_value() {
  local json value
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '<candidate-value>'
    return 0
  fi
  json="$(gq compute url-maps describe "$EDGE_MAP" --global --format=json)" ||
    die "cannot read the URL map $EDGE_MAP for the candidate route; run edge setup"
  value="$(printf '%s' "$json" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
for matcher in doc.get("pathMatchers") or []:
    for route in matcher.get("routeRules") or []:
        for rule in route.get("matchRules") or []:
            for header in rule.get("headerMatches") or []:
                if header.get("headerName") == sys.argv[1] and header.get("exactMatch"):
                    print(header["exactMatch"])
                    sys.exit(0)' "$CANDIDATE_HEADER_NAME")" || value=""
  case "$value" in
    "" | *[!0-9a-f]*) value="" ;;
  esac
  [ "${#value}" -eq 32 ] ||
    die "the URL map $EDGE_MAP has no random candidate route value; rerun scripts/deploy/gcp/deploy.sh $ENV_NAME edge setup"
  printf '%s' "$value"
}

# Remove the `candidate` tag (best effort, reported): its revision is either about to take all
# traffic or failed its smoke, and neither must stay reachable through the candidate backend.
candidate_close() {
  CANDIDATE_ARMED=0
  gc run services update-traffic "$API_SERVICE" --region="$REGION" --remove-tags=candidate ||
    say "WARNING: the candidate tag is still set; remove it: gcloud run services update-traffic $API_SERVICE --region=$REGION --remove-tags=candidate --project=$PROJECT"
}

# URL: refuse a deploy that would reopen run.app around the WAF or change the live JWT issuer.
live_guard() {
  local want="${JWT_ISSUER:-$1}" json live live_ingress live_issuer
  json="$(service_json)" || exit 1
  if [ -z "$json" ]; then
    say "live-state guard: skipped (no live service to compare$([ "$DRY_RUN" -eq 0 ] || printf '; dry run'))"
    return 0
  fi
  live="$(printf '%s' "$json" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
ingress = doc.get("metadata", {}).get("annotations", {}).get("run.googleapis.com/ingress", "")
issuer = ""
for container in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
    for item in container.get("env") or []:
        if item.get("name") == "JWT_ISSUER":
            issuer = item.get("value", "")
print(ingress + "\t" + issuer)')"
  live_ingress="${live%%	*}"
  live_issuer="${live#*	}"
  if [ "$live_ingress" = "$EDGE_LOCKED" ] && [ "$INGRESS" = "all" ] && [ "${DEPLOY_ALLOW_UNLOCK:-}" != "1" ]; then
    die "live ingress is locked; run edge unlock-ingress first, or this deploy reopens run.app around the WAF (DEPLOY_ALLOW_UNLOCK=1 overrides)"
  fi
  # Locking belongs to edge lock-ingress: it checks the SPA moved, probes run.app and every tag URL
  # and unlocks again on failure. A plain deploy must not lock a live service around those checks.
  if [ "$INGRESS" = "$EDGE_LOCKED" ] && [ "${live_ingress:-all}" != "$EDGE_LOCKED" ] && [ "${DEPLOY_ALLOW_LOCK:-}" != "1" ]; then
    die "live ingress is ${live_ingress:-all} but INGRESS=$EDGE_LOCKED: run edge lock-ingress first, or this deploy locks run.app without its checks (DEPLOY_ALLOW_LOCK=1 overrides)"
  fi
  if [ -n "$live_issuer" ] && [ "$live_issuer" != "$want" ] && [ "${DEPLOY_ALLOW_ISSUER_CHANGE:-}" != "1" ]; then
    die "this deploy changes JWT_ISSUER from $live_issuer to $want and signs every user out: pin JWT_ISSUER=$live_issuer in scripts/deploy/gcp/$ENV_NAME.env (DEPLOY_ALLOW_ISSUER_CHANGE=1 overrides)"
  fi
  say "live-state guard: OK (ingress ${live_ingress:-unknown}, issuer unchanged)"
}

# NAME NEG: a logged EXTERNAL_MANAGED backend service with the NEG and the Armor policy.
edge_backend() {
  local name="$1" neg="$2" groups
  gq compute backend-services describe "$name" --global --format='value(name)' >/dev/null ||
    gc compute backend-services create "$name" --global --load-balancing-scheme=EXTERNAL_MANAGED \
      --enable-logging --logging-sample-rate=1
  groups="$(gq compute backend-services describe "$name" --global --format='value(backends[].group)' || true)"
  if ! printf '%s\n' "$groups" | tr ';' '\n' | grep -q "/networkEndpointGroups/$neg\$"; then
    gc compute backend-services add-backend "$name" --global --network-endpoint-group="$neg" \
      --network-endpoint-group-region="$REGION"
  fi
  gc compute backend-services update "$name" --global --security-policy="$EDGE_POLICY" \
    --enable-logging --logging-sample-rate=1
}

# FILE VALUE: the URL map. Route rules live only inside a path matcher a host rule points to. Only
# the header `x-aimos-candidate: VALUE` goes to the candidate backend, everything else to the main
# one. VALUE is random and never printed (ADR-0022): a deploy reads it back from the URL map.
edge_url_map() {
  local be="projects/$PROJECT/global/backendServices"
  cat >"$1" <<YAML
name: $EDGE_MAP
defaultService: $be/$EDGE_BE
hostRules:
- hosts: ['*']
  pathMatcher: edge
pathMatchers:
- name: edge
  defaultService: $be/$EDGE_BE
  routeRules:
  - priority: 1
    matchRules:
    - prefixMatch: /
      headerMatches:
      - headerName: $CANDIDATE_HEADER_NAME
        exactMatch: '$2'
    service: $be/$EDGE_CANDIDATE_BE
YAML
  say "url map $1 (candidate value redacted):"
  sed -e "s/'$2'/'<redacted>'/" -e 's/^/    /' "$1"
}

edge_setup() {
  local neg ip value
  # shellcheck disable=SC2086
  gc services enable $EDGE_APIS
  gq compute addresses describe "$EDGE_IP" --global --format='value(address)' >/dev/null ||
    gc compute addresses create "$EDGE_IP" --global --ip-version=IPV4 --network-tier=PREMIUM
  gq compute network-endpoint-groups describe "$EDGE_NEG" --region="$REGION" --format='value(name)' >/dev/null ||
    gc compute network-endpoint-groups create "$EDGE_NEG" --region="$REGION" \
      --network-endpoint-type=serverless --cloud-run-service="$API_SERVICE"
  gq compute network-endpoint-groups describe "$EDGE_CANDIDATE_NEG" --region="$REGION" --format='value(name)' >/dev/null ||
    gc compute network-endpoint-groups create "$EDGE_CANDIDATE_NEG" --region="$REGION" \
      --network-endpoint-type=serverless --cloud-run-service="$API_SERVICE" --cloud-run-tag=candidate
  gq compute security-policies describe "$EDGE_POLICY" --format='value(name)' >/dev/null ||
    gc compute security-policies create "$EDGE_POLICY" --type=CLOUD_ARMOR \
      --description="API edge WAF (armor-rules.yaml)"
  gc compute security-policies update "$EDGE_POLICY" --json-parsing=STANDARD --log-level=NORMAL \
    --enable-layer7-ddos-defense
  for neg in "$EDGE_BE:$EDGE_NEG" "$EDGE_CANDIDATE_BE:$EDGE_CANDIDATE_NEG"; do
    edge_backend "${neg%%:*}" "${neg#*:}"
  done
  # A fresh random candidate route value on every run (rerun edge setup to rotate it).
  value="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"
  [ "${#value}" -eq 32 ] || die "cannot read 16 random bytes for the candidate route"
  edge_url_map "$WORK/url-map.yaml" "$value"
  gc compute url-maps import "$EDGE_MAP" --global --source="$WORK/url-map.yaml"
  if gq compute ssl-policies describe "$EDGE_SSL" --global --format='value(name)' >/dev/null; then
    gc compute ssl-policies update "$EDGE_SSL" --global --profile=MODERN --min-tls-version=1.2
  else
    gc compute ssl-policies create "$EDGE_SSL" --global --profile=MODERN --min-tls-version=1.2
  fi
  gq compute ssl-certificates describe "$EDGE_CERT" --global --format='value(name)' >/dev/null ||
    gc compute ssl-certificates create "$EDGE_CERT" --global --domains="$EDGE_DOMAIN"
  if gq compute target-https-proxies describe "$EDGE_PROXY" --global --format='value(name)' >/dev/null; then
    gc compute target-https-proxies update "$EDGE_PROXY" --global --ssl-policy="$EDGE_SSL"
  else
    gc compute target-https-proxies create "$EDGE_PROXY" --global --url-map="$EDGE_MAP" \
      --ssl-certificates="$EDGE_CERT" --ssl-policy="$EDGE_SSL"
  fi
  gq compute forwarding-rules describe "$EDGE_RULE" --global --format='value(name)' >/dev/null ||
    gc compute forwarding-rules create "$EDGE_RULE" --global --load-balancing-scheme=EXTERNAL_MANAGED \
      --network-tier=PREMIUM --address="$EDGE_IP" --target-https-proxy="$EDGE_PROXY" --ports=443
  ip="$(gq compute addresses describe "$EDGE_IP" --global --format='value(address)' || true)"
  say "point an A record for $EDGE_DOMAIN at ${ip:-<IP>}; the name carries only that A record, and any"
  say "CAA record on the apex must allow pki.goog or letsencrypt.org (Google-managed certificate)."
  say "next: scripts/deploy/gcp/deploy.sh $ENV_NAME edge rules (every rule in preview)"
}

# MODE (rules | enforce): converge kuinn-api-waf to armor-rules.yaml rule by rule, then check it.
edge_rules() {
  local mode="$1" rc=0 line
  py "$HERE/armor_rules.py" check "$EDGE_RULES" --enforce "$EDGE_ENFORCE" ||
    die "rules: armor-rules.yaml or EDGE_ENFORCE='$EDGE_ENFORCE' is invalid (see above)"
  if [ "$mode" = "enforce" ]; then
    py "$HERE/armor_rules.py" summary "$EDGE_RULES" --enforce "$EDGE_ENFORCE"
  fi
  if gq compute security-policies export "$EDGE_POLICY" --file-name="$WORK/live.json" \
    --file-format=json >/dev/null; then
    py "$HERE/armor_rules.py" plan "$EDGE_RULES" --enforce "$EDGE_ENFORCE" \
      --live "$WORK/live.json" --out "$WORK/argv.txt" || rc=$?
  elif [ "$DRY_RUN" -eq 1 ]; then
    say "rules: dry run, planned from an empty policy"
    py "$HERE/armor_rules.py" plan "$EDGE_RULES" --enforce "$EDGE_ENFORCE" \
      --out "$WORK/argv.txt" || rc=$?
  else
    die "rules: cannot export $EDGE_POLICY; run scripts/deploy/gcp/deploy.sh $ENV_NAME edge setup first"
  fi
  if [ "$rc" -eq 3 ]; then
    say "rules: $EDGE_POLICY already matches armor-rules.yaml"
    return 0
  fi
  [ "$rc" -eq 0 ] || die "rules: the plan failed (exit $rc)"
  while IFS= read -r line <&3; do
    # One shell-quoted argv per line, written by armor_rules.py (shlex.join).
    eval "set -- $line"
    gc "$@" || die "rules: partial apply at priority $5; rerun edge rules to converge"
  done 3<"$WORK/argv.txt"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "rules: dry run, nothing was changed"
    return 0
  fi
  gq compute security-policies export "$EDGE_POLICY" --file-name="$WORK/after.json" \
    --file-format=json >/dev/null || die "rules: cannot export $EDGE_POLICY after the apply"
  py "$HERE/armor_rules.py" verify "$EDGE_RULES" --enforce "$EDGE_ENFORCE" --live "$WORK/after.json" ||
    die "rules: $EDGE_POLICY does not match armor-rules.yaml after the apply (priorities above); rerun edge $mode"
  say "rules: $EDGE_POLICY matches armor-rules.yaml (enforced: ${EDGE_ENFORCE:-none})"
}

# The prod SPA origins (ALLOWED_ORIGINS), one per line.
spa_origins() { printf '%s\n' "$ALLOWED_ORIGINS" | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -v '^$' || true; }

# ORIGIN: the edge /readyz answers that origin with postgres ok and its CORS grant; a grace
# rewrite on the origin that still answers JSON answers postgres ok too. Sets LOCK_FAIL.
spa_readyz() {
  local origin="$1" body
  show curl -sS -D "$WORK/edge-h" -H "Origin: $origin" "https://$EDGE_DOMAIN/readyz"
  show curl -sS -D "$WORK/spa-h" "$origin/readyz"
  [ "$DRY_RUN" -eq 0 ] || return 0
  : >"$WORK/edge-h"
  body="$(curl -sS --max-time 30 -D "$WORK/edge-h" -H "Origin: $origin" "https://$EDGE_DOMAIN/readyz" || true)"
  case "$body" in
    *'"postgres":"ok"'*) ;;
    *) LOCK_FAIL="$origin: https://$EDGE_DOMAIN/readyz did not answer postgres ok"; return 1 ;;
  esac
  if ! tr -d '\r' <"$WORK/edge-h" | grep -qixF "access-control-allow-origin: $origin"; then
    LOCK_FAIL="$origin: https://$EDGE_DOMAIN/readyz sent no Access-Control-Allow-Origin for it"
    return 1
  fi
  : >"$WORK/spa-h"
  body="$(curl -sS --max-time 30 -D "$WORK/spa-h" "$origin/readyz" || true)"
  if tr -d '\r' <"$WORK/spa-h" | grep -qi '^content-type: *application/json'; then
    case "$body" in
      *'"postgres":"ok"'*) ;;
      *) LOCK_FAIL="$origin: the grace rewrite $origin/readyz did not answer postgres ok"; return 1 ;;
    esac
  fi
}

# Every SPA origin serves a bundle built for the edge and a CSP that allows it. Sets LOCK_FAIL.
spa_moved() {
  local origin page bundle csp code
  for origin in $(spa_origins); do
    show curl -sS -D "$WORK/spa-h" "$origin/"
    if [ "$DRY_RUN" -eq 0 ]; then
      : >"$WORK/spa-h"
      page="$(curl -sS --max-time 30 -D "$WORK/spa-h" "$origin/" || true)"
      bundle="$(printf '%s' "$page" | grep -o '/assets/[A-Za-z0-9._-]*\.js' | head -n 1 || true)"
      [ -n "$bundle" ] || { LOCK_FAIL="$origin: no /assets/*.js bundle in $origin/"; return 1; }
      csp="$(tr -d '\r' <"$WORK/spa-h" | grep -i '^content-security-policy:' | head -n 1 |
        sed 's/^[^:]*://' | tr ';' '\n' | grep -i '^ *connect-src ' || true)"
      case " $csp " in
        *" https://$EDGE_DOMAIN "* | *" https://$EDGE_DOMAIN/ "*) ;;
        *) LOCK_FAIL="$origin: the Content-Security-Policy connect-src lacks https://$EDGE_DOMAIN"; return 1 ;;
      esac
      code="$(curl -sS --max-time 30 "$origin$bundle" || true)"
      case "$code" in
        *"https://$EDGE_DOMAIN"*) ;;
        *) LOCK_FAIL="$origin: the bundle $bundle does not call https://$EDGE_DOMAIN (VITE_API_ORIGIN)"; return 1 ;;
      esac
    fi
    spa_readyz "$origin" || return 1
  done
}

# URL...: each answers /api/v1/auth/me with the front end's ingress refusal, 403 or 404. A 401
# (the API), a network failure (000), an empty URL or anything else is not proof: LOCK_FAIL.
refused_direct() {
  local url code n
  for url in "$@"; do
    [ -n "$url" ] || { LOCK_FAIL="the service has no run.app URL to probe"; return 1; }
    n=0
    while :; do
      code="$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' "$url/api/v1/auth/me" || true)"
      case "$code" in 403 | 404) break ;; esac
      n=$((n + 1))
      if [ "$n" -ge "$SMOKE_TRIES" ]; then
        if [ "$code" = "401" ]; then
          LOCK_FAIL="$url still answers the API directly"
        else
          LOCK_FAIL="$url answered '${code:-nothing}', not the ingress refusal (403/404)"
        fi
        return 1
      fi
      sleep "$SMOKE_SLEEP"
    done
  done
}

edge_lock() {
  local json inventory stale="" urls="" direct tag url origin
  [ "$INGRESS" = "$EDGE_LOCKED" ] ||
    die "set INGRESS=$EDGE_LOCKED in scripts/deploy/gcp/$ENV_NAME.env first, or the next deploy unlocks it"
  check_ingress
  smoke "https://$EDGE_DOMAIN" || die "lock refused: the load balancer smoke failed for https://$EDGE_DOMAIN"
  LOCK_FAIL=""
  spa_moved || die "lock refused: $LOCK_FAIL"
  json="$(service_json)" || exit 1
  if [ -n "$json" ]; then
    inventory="$(printf '%s' "$json" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
print("url\t" + doc.get("status", {}).get("url", ""))
for item in doc.get("status", {}).get("traffic", []):
    if item.get("tag"):
        print(item["tag"] + "\t" + item.get("url", ""))')"
    direct="$(printf '%s\n' "$inventory" | sed -n 's/^url	//p')"
    while IFS='	' read -r tag url; do
      case "$tag" in
        url | "") ;;
        sha-*) stale="${stale:+$stale,}$tag" ;;
        *) urls="$urls $url" ;;
      esac
    done <<EOF
$inventory
EOF
  elif [ "$DRY_RUN" -eq 0 ]; then
    die "lock refused: no service $API_SERVICE in $PROJECT"
  fi
  [ -z "$stale" ] || gc run services update-traffic "$API_SERVICE" --region="$REGION" --remove-tags="$stale"
  gc run services update "$API_SERVICE" --region="$REGION" --ingress="$EDGE_LOCKED"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "lock: dry run; a real run then probes run.app and every tag URL and rechecks /readyz"
    return 0
  fi
  # shellcheck disable=SC2086
  if refused_direct "$direct" $urls; then
    for origin in $(spa_origins); do
      spa_readyz "$origin" || break
    done
  fi
  if [ -n "$LOCK_FAIL" ]; then
    gc run services update "$API_SERVICE" --region="$REGION" --ingress=all
    die "lock rolled back: $LOCK_FAIL; INGRESS is still locked in scripts/deploy/gcp/$ENV_NAME.env, set it back to all"
  fi
  say "lock: $API_SERVICE takes traffic only through the load balancer; run.app and tag URLs are refused"
}

edge_unlock() {
  gc run services update "$API_SERVICE" --region="$REGION" --ingress=all
  say "unlocked: set INGRESS=all in scripts/deploy/gcp/$ENV_NAME.env too"
}

# DISPLAY_NAME: the alert policy's resource name, or empty.
policy_name() {
  gq monitoring policies list --filter="displayName=\"$1\"" --format='value(name)' || true
}

edge_alerts() {
  local channel uptime count file name existing threshold metric current
  case "$ALERT_EMAIL" in
    "") die "alerts: set ALERT_EMAIL in scripts/deploy/gcp/$ENV_NAME.env (one address)" ;;
    *[,\ ]* | *@*@*) die "alerts: ALERT_EMAIL must be one address, not '$ALERT_EMAIL'" ;;
    ?*@?*.?*) ;;
    *) die "alerts: ALERT_EMAIL must be an address (x@y.z), not '$ALERT_EMAIL'" ;;
  esac
  if [ "$DRY_RUN" -eq 0 ] && ! gcloud beta monitoring channels create --help >/dev/null 2>&1; then
    die "alerts: this gcloud has no beta monitoring commands; run: gcloud components install beta"
  fi
  # Logs-based metrics.
  for metric in \
    "edge_armor_deny|resource.type=\"http_load_balancer\" AND jsonPayload.enforcedSecurityPolicy.outcome=\"DENY\"" \
    "edge_rate_429|resource.type=\"http_load_balancer\" AND httpRequest.status=429"; do
    gq logging metrics describe "${metric%%|*}" --format='value(name)' >/dev/null ||
      gc logging metrics create "${metric%%|*}" --description="API edge (DPP-2)" --log-filter="${metric#*|}"
  done
  # The email channel (no GA create command).
  channel="$(gq beta monitoring channels list --filter="displayName=\"$EDGE_CHANNEL\"" --format='value(name)' || true)"
  if [ -z "$channel" ]; then
    gc beta monitoring channels create --type=email --channel-labels=email_address="$ALERT_EMAIL" \
      --display-name="$EDGE_CHANNEL"
    channel="$(gq beta monitoring channels list --filter="displayName=\"$EDGE_CHANNEL\"" --format='value(name)' || true)"
  else
    # The channel exists: make it send to today's ALERT_EMAIL, not the address it was made with.
    channel="$(printf '%s\n' "$channel" | head -n 1)"
    current="$(gq beta monitoring channels describe "$channel" --format='value(labels.email_address)' || true)"
    if [ "$current" != "$ALERT_EMAIL" ]; then
      gc beta monitoring channels update "$channel" --update-channel-labels=email_address="$ALERT_EMAIL"
    fi
  fi
  [ -n "$channel" ] || [ "$DRY_RUN" -eq 0 ] || channel="projects/$PROJECT/notificationChannels/<channel-id>"
  [ -n "$channel" ] || die "alerts: cannot find the $EDGE_CHANNEL channel after creating it"
  channel="$(printf '%s\n' "$channel" | head -n 1)"
  # The uptime check first: its alert policy filters on the id Google assigns, not the name.
  uptime="$(gq monitoring uptime list-configs --filter="displayName=$EDGE_UPTIME" --format='value(name.basename())' || true)"
  if [ -z "$uptime" ]; then
    gc monitoring uptime create "$EDGE_UPTIME" --resource-type=uptime-url \
      --resource-labels="host=$EDGE_DOMAIN,project_id=$PROJECT" --path=/readyz --protocol=https --period=5
    uptime="$(gq monitoring uptime list-configs --filter="displayName=$EDGE_UPTIME" --format='value(name.basename())' || true)"
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    uptime="<uptime-check-id>"
  else
    count="$(printf '%s' "$uptime" | grep -c . || true)"
    [ "$count" -eq 1 ] || die "alerts: want exactly one uptime check named $EDGE_UPTIME, found $count"
  fi
  CHANNEL="$channel" UPTIME="$uptime" DENY="$EDGE_DENY_ALERT" R429="$EDGE_429_ALERT" \
    RATIO="$EDGE_5XX_RATIO" RULE="$EDGE_RULE" OUT="$WORK" python3 -c '
import json, os
e = os.environ
def threshold(name, flt, value, denominator=None):
    cond = {"filter": flt, "comparison": "COMPARISON_GT", "thresholdValue": value, "duration": "0s",
            "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_SUM",
                              "crossSeriesReducer": "REDUCE_SUM"}]}
    if denominator:
        cond["denominatorFilter"] = denominator
        cond["denominatorAggregations"] = cond["aggregations"]
    return {"displayName": name, "conditionThreshold": cond}
lb = "metric.type=\"loadbalancing.googleapis.com/https/request_count\" AND resource.type=\"https_lb_rule\" AND resource.labels.forwarding_rule_name=\"%s\"" % e["RULE"]
policies = {
  "deny": ("aimos-edge armor deny spike", threshold("Armor denies over %s in 5 min" % e["DENY"],
           "metric.type=\"logging.googleapis.com/user/edge_armor_deny\"", float(e["DENY"]))),
  "429": ("aimos-edge 429 spike", threshold("429s over %s in 5 min" % e["R429"],
          "metric.type=\"logging.googleapis.com/user/edge_rate_429\"", float(e["R429"]))),
  "5xx": ("aimos-edge lb 5xx", threshold("5xx over %s of requests in 5 min" % e["RATIO"],
          lb + " AND metric.labels.response_code_class=500", float(e["RATIO"]), lb)),
  "uptime": ("aimos-edge uptime failing", {"displayName": "/readyz failing from 2+ regions for 5 min",
    "conditionThreshold": {
      "filter": "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.labels.check_id=\"%s\"" % e["UPTIME"],
      "comparison": "COMPARISON_GT", "thresholdValue": 1, "duration": "300s",
      "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_NEXT_OLDER",
                        "crossSeriesReducer": "REDUCE_COUNT_FALSE", "groupByFields": ["resource.label.*"]}]}}),
  "ddos": ("aimos-edge adaptive protection", {"displayName": "Adaptive Protection attack alert",
    "conditionMatchedLog": {"filter": "resource.type=\"network_security_policy\" AND logName:\"networksecurity.googleapis.com%2Fdos_attack\""}}),
}
for key, (name, condition) in policies.items():
    doc = {"displayName": name, "combiner": "OR", "enabled": True, "conditions": [condition],
           "notificationChannels": [e["CHANNEL"]]}
    if "conditionMatchedLog" in condition:
        doc["alertStrategy"] = {"notificationRateLimit": {"period": "300s"}}
    with open(os.path.join(e["OUT"], "policy-%s.json" % key), "w") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")'
  for name in deny 429 5xx uptime ddos; do
    file="$WORK/policy-$name.json"
    say "policy $file:"
    sed 's/^/    /' "$file"
    existing="$(policy_name "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["displayName"])' "$file")")"
    if [ -z "$existing" ]; then
      gc monitoring policies create --policy-from-file="$file"
    elif [ "$name" = "deny" ]; then
      threshold="$(gq monitoring policies describe "$existing" \
        --format='value(conditions[0].conditionThreshold.thresholdValue)' || true)"
      if [ "${threshold%.0}" != "$EDGE_DENY_ALERT" ]; then
        gc monitoring policies update "$existing" --policy-from-file="$file"
      fi
    fi
  done
  # The 400-day security bucket and its sink. OAuth callback URLs carry code/state: never sunk.
  ensure_security_bucket
  local filter dest
  filter='(resource.type="http_load_balancer" AND (jsonPayload.enforcedSecurityPolicy.outcome="DENY" OR jsonPayload.previewSecurityPolicy.outcome="DENY" OR (httpRequest.requestUrl=~"/api/v1/(auth|signup)" AND httpRequest.status>=400)) AND NOT httpRequest.requestUrl:"/callback") OR (resource.type="network_security_policy" AND logName:"networksecurity.googleapis.com%2Fdos_attack")'
  dest="logging.googleapis.com/projects/$PROJECT/locations/global/buckets/$EDGE_LOG_BUCKET"
  if gq logging sinks describe "$EDGE_SINK" --format='value(name)' >/dev/null; then
    gc logging sinks update "$EDGE_SINK" "$dest" --log-filter="$filter"
  else
    gc logging sinks create "$EDGE_SINK" "$dest" --log-filter="$filter"
  fi
  existing="$(gq logging sinks describe _Default --format='value(exclusions[].name)' || true)"
  case ";$existing;" in
    *";edge-callback-urls;"*) ;;
    *) gc logging sinks update _Default \
      --add-exclusion='name=edge-callback-urls,filter=resource.type="http_load_balancer" AND httpRequest.requestUrl:"/callback"' ;;
  esac
  say "alerts: metrics, channel, uptime check, 5 policies, $EDGE_LOG_BUCKET (400 days) and its sink are in place"
}

edge_status() {
  local export left code
  gq compute addresses describe "$EDGE_IP" --global --format='value(address)' || true
  gq compute ssl-certificates describe "$EDGE_CERT" --global \
    --format='value(managed.status,managed.domainStatus)' || true
  gq run services describe "$API_SERVICE" --region="$REGION" \
    --format='value(metadata.annotations."run.googleapis.com/ingress")' || true
  gq compute security-policies describe "$EDGE_POLICY" \
    --format='table(rules[].priority,rules[].preview,rules[].action)' || true
  if export="$(gq compute security-policies export "$EDGE_POLICY" --file-name=/dev/stdout --file-format=yaml)"; then
    say "policy export (DPP 1.1.3 evidence):"
    printf '%s\n' "$export" | sed 's/^/    /'
    left="$(printf '%s\n' "$export" | grep -c '^ *preview: true' || true)"
    say "preview rules left: $left"
  else
    say "preview rules left: unknown (no export)"
  fi
  show curl -sS -o /dev/null -w '%{http_code}' "https://$EDGE_DOMAIN/api/v1/auth/me"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "edge serving: unknown (dry run)"
    return 0
  fi
  code="$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' "https://$EDGE_DOMAIN/api/v1/auth/me" || true)"
  if [ "$code" = "401" ]; then
    say "edge serving: yes (a 401 from https://$EDGE_DOMAIN with a valid certificate)"
  else
    say "edge serving: no ('$code')"
  fi
}

cmd_edge() {
  [ ${#REST[@]} -ge 1 ] || usage
  local sub="${REST[0]}"
  [ -n "$EDGE_DOMAIN" ] || die "edge needs EDGE_DOMAIN (scripts/deploy/gcp/$ENV_NAME.env); staging has no domain"
  case "$sub" in
    setup) edge_setup ;;
    rules) edge_rules rules ;;
    enforce) edge_rules enforce ;;
    lock-ingress) edge_lock ;;
    unlock-ingress) edge_unlock ;;
    alerts) edge_alerts ;;
    cors) check_filled; bucket_cors ;;
    status) edge_status ;;
    *) usage ;;
  esac
}

# ---- status ----
cmd_status() {
  local url job
  url="$(service_url || true)"
  say "service $API_SERVICE: ${url:-not deployed}"
  gq run services describe "$API_SERVICE" --region="$REGION" \
    --format='value(status.latestReadyRevisionName,status.latestCreatedRevisionName)' || true
  gq run revisions list --service="$API_SERVICE" --region="$REGION" --limit=5 \
    --format='table(metadata.name,status.conditions[0].status,metadata.creationTimestamp)' || true
  gq run worker-pools describe "$WORKER_POOL" --region="$REGION" --format='value(name)' || true
  for job in "$DB_INIT_JOB" "$MIGRATE_JOB" "$OPS_JOB" "$MAINT_JOB"; do
    gq run jobs executions list --job="$job" --region="$REGION" --limit=3 \
      --format='table(metadata.name,status.succeededCount,status.failedCount,status.completionTime)' || true
  done
  say "secrets (names only):"
  gq secrets list --format='value(name.basename())' || true
}

case "$CMD" in
  preflight) cmd_preflight ;;
  setup) cmd_setup ;;
  build) cmd_preflight; cmd_build ;;
  db-init) cmd_db_init ;;
  migrate) cmd_migrate ;;
  deploy) cmd_deploy ;;
  release) cmd_preflight; cmd_release ;;
  smoke) cmd_smoke ;;
  tenant) cmd_tenant ;;
  status) cmd_status ;;
  edge) cmd_edge ;;
  *) usage ;;
esac
