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
#   migrate     the migrate job (alembic upgrade head, dictionary sync), waited on
#   deploy      the api service and the worker pool, then smoke
#   release     build, migrate, deploy
#   smoke       GET /healthz and /readyz on the service URL
#   tenant      create | add-member | set-password through the staff CLI, in a one-off job
#   status      service URL, revisions, job runs, secret names
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
USER_SECRETS="SENTRY_DSN OPENAI_API_KEY"
REQUIRED_APIS="run.googleapis.com sqladmin.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com cloudkms.googleapis.com storage.googleapis.com iam.googleapis.com logging.googleapis.com cloudresourcemanager.googleapis.com serviceusage.googleapis.com"
API_SECRETS="DATABASE_URL JWT_SECRET ADMIN_SESSION_SECRET S3_ACCESS_KEY S3_SECRET_KEY SENTRY_DSN"
WORKER_SECRETS="DATABASE_URL S3_ACCESS_KEY S3_SECRET_KEY SENTRY_DSN OPENAI_API_KEY"
MIGRATE_SECRETS="ALEMBIC_DATABASE_URL"
DB_INIT_SECRETS="POSTGRES_PASSWORD APP_RW_PASSWORD APP_RO_PASSWORD MIGRATOR_PASSWORD"
OPS_SECRETS="ALEMBIC_DATABASE_URL"
PW_MOUNT="/secrets/initial-password/value"

usage() {
  sed -n '5,17p' "$0" | sed 's/^# \{0,1\}//' >&2
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

LABELS="app=aimos,env=$APP_ENV"
CONN="$PROJECT:$REGION:$SQL_INSTANCE"
AR_HOST="$REGION-docker.pkg.dev"
IMAGE="$AR_HOST/$PROJECT/$AR_REPO/$IMAGE_NAME"
KMS_KEY_URI="projects/$PROJECT/locations/$REGION/keyRings/$KMS_KEYRING/cryptoKeys/$KMS_KEY"
S3_ENDPOINT_URL="https://storage.googleapis.com"
if [ "$AMAZON_USE_SANDBOX" = "true" ]; then
  AMAZON_SPAPI_ENDPOINT="https://sandbox.sellingpartnerapi-eu.amazon.com"
else
  AMAZON_SPAPI_ENDPOINT="https://sellingpartnerapi-eu.amazon.com"
fi
sa_email() { printf '%s@%s.iam.gserviceaccount.com' "$1" "$PROJECT"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/aimos-deploy.XXXXXX")"
chmod 0700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

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
setup_apis() {
  # shellcheck disable=SC2086
  gc services enable $REQUIRED_APIS
  gc logging buckets update _Default --location=global --retention-days="$LOG_RETENTION_DAYS"
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
    "$SA_OPS:one-off staff jobs" "$SA_STORAGE:GCS HMAC identity" "$SA_BUILD:Cloud Build"; do
    name="${pair%%:*}"
    gq iam service-accounts describe "$(sa_email "$name")" --format='value(email)' >/dev/null ||
      gc iam service-accounts create "$name" --display-name="aimos ${pair#*:}"
  done
  for name in "$SA_API" "$SA_WORKER" "$SA_MIGRATE" "$SA_OPS"; do
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
  # The dashboard PUTs to and GETs from V4 signed URLs straight from the browser (S17b).
  ALLOWED_ORIGINS="$ALLOWED_ORIGINS" python3 -c '
import json, os
origins = [o.strip() for o in os.environ["ALLOWED_ORIGINS"].split(",") if o.strip()]
print(json.dumps([{"origin": origins, "method": ["GET", "HEAD", "PUT"],
                   "responseHeader": ["Content-Type"], "maxAgeSeconds": 3600}]))' >"$WORK/cors.json"
  for bucket in "$BUCKET_IMAGES" "$BUCKET_CSV"; do
    gc storage buckets update "gs://$bucket" --cors-file="$WORK/cors.json"
  done
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
  else
    gc sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_17 --edition=ENTERPRISE \
      --tier="$SQL_TIER" --region="$REGION" --availability-type=zonal --storage-type=SSD \
      --storage-size="${SQL_STORAGE_GB}GB" --storage-auto-increase \
      --backup-start-time=20:30 --backup-location="$REGION" --retained-backups-count=7 \
      --enable-point-in-time-recovery --retained-transaction-log-days="$SQL_PITR_DAYS" \
      --retain-backups-on-delete --maintenance-window-day=SUN --maintenance-window-hour=21 \
      --database-flags="$(sql_flags)" --insights-config-query-insights-enabled \
      --assign-ip --connector-enforcement=REQUIRED --ssl-mode=ENCRYPTED_ONLY \
      "$protection" --labels="$LABELS" --async
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
}

# The two values only a person can supply. Each missing one gets a command for the user's OWN
# terminal: `read -s` keeps the value off the screen, out of argv and out of shell history.
check_user_secrets() {
  local name missing=0 wanted="SENTRY_DSN"
  # Only the worker embeds (S16): a deployment without the worker pool needs no OpenAI key yet.
  [ "$WORKER_ENABLED" != "true" ] || wanted="$wanted OPENAI_API_KEY"
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
  [ "$role" != "api" ] || printf 'JWT_ISSUER\t%s\n' "$url"
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

deploy_api() {
  local image="$1" url="$2" tag existed=0 now
  local -a args
  tag="sha-$(git_sha)"
  env_file api "$url" "$WORK/api-env.yaml"
  if service_url >/dev/null; then existed=1; fi
  args=(run deploy "$API_SERVICE" --image="$image" --region="$REGION"
    --service-account="$(sa_email "$SA_API")" --allow-unauthenticated --ingress=all --port=8080
    --cpu="$API_CPU" --memory="$API_MEMORY" --min-instances="$API_MIN_INSTANCES"
    --max-instances="$API_MAX_INSTANCES" --concurrency="$API_CONCURRENCY" --cpu-boost
    --execution-environment=gen2 --set-cloudsql-instances="$CONN"
    --env-vars-file="$WORK/api-env.yaml" --set-secrets="$(secret_refs "$API_SECRETS")"
    --labels="$LABELS")
  if [ "$existed" -eq 1 ]; then
    # ADR-0001: the new revision takes no traffic until its tagged URL passes the smoke test.
    gc "${args[@]}" --no-traffic --tag="$tag"
    if [ "$DRY_RUN" -eq 0 ]; then
      smoke "$(tag_url "$tag")" || die "revision $tag failed smoke; traffic stays on the previous revision"
    fi
    gc run services update-traffic "$API_SERVICE" --region="$REGION" --to-latest
  else
    gc "${args[@]}"
    # JWT_ISSUER and the OAuth redirect URIs must be the URL the service actually got.
    if [ "$DRY_RUN" -eq 0 ] && [ -z "$API_PUBLIC_URL" ]; then
      now="$(service_url || true)"
      if [ -n "$now" ] && [ "$now" != "$url" ]; then
        say "service URL is $now (not $url): updating JWT_ISSUER and the redirect URIs"
        gc run services update "$API_SERVICE" --region="$REGION" \
          --update-env-vars="^|^JWT_ISSUER=$now|AMAZON_OAUTH_REDIRECT_URI=$now/api/v1/marketplaces/amazon/callback|FLIPKART_OAUTH_REDIRECT_URI=$now/api/v1/marketplaces/flipkart/callback"
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

cmd_deploy() {
  check_filled
  local image url
  image="$(image_ref)"
  url="$(api_url)"
  deploy_api "$image" "$url"
  [ "$DRY_RUN" -eq 1 ] || [ -n "$API_PUBLIC_URL" ] || url="$(service_url)"
  deploy_worker "$image" "$url"
  cmd_smoke
}

# ---- smoke ----
# URL: /healthz 200, then /readyz 200 with postgres ok (retried: staging scales to zero).
smoke() {
  local url="$1" code body n=0
  show curl -sS -o /dev/null -w '%{http_code}' "$url/healthz"
  show curl -sS "$url/readyz"
  [ "$DRY_RUN" -eq 0 ] || return 0
  until code="$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' "$url/healthz")" && [ "$code" = "200" ]; do
    n=$((n + 1))
    [ "$n" -lt 10 ] || { say "smoke: /healthz answered '$code'"; return 1; }
    sleep 6
  done
  n=0
  until body="$(curl -sS --max-time 30 "$url/readyz")" && case "$body" in *'"postgres":"ok"'*) true ;; *) false ;; esac; do
    n=$((n + 1))
    [ "$n" -lt 10 ] || { say "smoke: /readyz answered '$body'"; return 1; }
    sleep 6
  done
  say "smoke OK: $url (/healthz 200, /readyz postgres ok)"
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

# EMAIL FRESH: the user's initial-password secret; FRESH=1 adds a new version even if one exists.
ensure_password() {
  local secret
  secret="$(password_secret "$1")"
  ensure_secret "$secret"
  if [ "$2" -eq 1 ] || ! secret_has_version "$secret"; then
    pipe_note "openssl rand -hex 16 | gcloud secrets versions add $secret --data-file=- --project=$PROJECT"
    if [ "$DRY_RUN" -eq 0 ]; then
      openssl rand -hex 16 | tr -d '\n' |
        gcloud secrets versions add "$secret" --data-file=- --project="$PROJECT" --quiet
    fi
  fi
  grant_secret "$secret" "$SA_OPS"
}

# EMAIL NOTE CLI_ARGS...: run `python -m app.cli CLI_ARGS` with the user's password secret on
# stdin. The entrypoint's shell role runs `python "$@"`; stdin is the mounted secret file.
ops_job() {
  local email="$1" note="$2" secret image args arg
  shift 2
  secret="$(password_secret "$email")"
  image="$(image_ref)"
  args='-c|import runpy, sys; sys.stdin = open(sys.argv[1], encoding="utf-8"); sys.argv = ["app.cli", *sys.argv[2:]]; runpy.run_module("app.cli", run_name="__main__", alter_sys=True)'
  args="$args|$PW_MOUNT"
  for arg in "$@"; do
    case "$arg" in *"|"*) die "an argument may not contain '|'" ;; esac
    args="$args|$arg"
  done
  gc run jobs deploy "$OPS_JOB" --image="$image" --region="$REGION" \
    --service-account="$(sa_email "$SA_OPS")" --set-cloudsql-instances="$CONN" \
    --set-env-vars="APP_ENV=$APP_ENV,APP_ROLE=shell,LOG_FORMAT=json" \
    --set-secrets="ALEMBIC_DATABASE_URL=ALEMBIC_DATABASE_URL:latest,$PW_MOUNT=$secret:latest" \
    --args="^|^$args" --tasks=1 --max-retries=0 --task-timeout=300s --cpu=1 --memory=1Gi \
    --labels="$LABELS"
  run_job "$OPS_JOB"
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
  [ ${#REST[@]} -ge 1 ] || die "usage: tenant create --name N --slug S --owner-email E --staff-email F [--trial-days D] | add-member --slug S --email E --role owner|operator|reviewer|admin --staff-email F | set-password --email E --staff-email F | make-admin --email E --staff-email F"
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
    *) die "unknown tenant action '$action' (create | add-member | set-password | make-admin)" ;;
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
  for job in "$DB_INIT_JOB" "$MIGRATE_JOB" "$OPS_JOB"; do
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
  *) usage ;;
esac
