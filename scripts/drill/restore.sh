#!/usr/bin/env bash
# The restore drill (S26 T19; AC 8; PLAN D11 as amended by CODE-PLAN C4): `make drill-restore`.
#
#   scripts/drill/restore.sh [--stack STACK_DIR] [--seed-run]
#
# Source: a fresh-stack database (scripts/e2e/lib/fresh_stack.sh). With --stack, an acceptance
# stack that is already up (the acceptance run done there); otherwise this script brings up its
# own (profile acceptance) and, with --seed-run, runs the v1 acceptance run on it first
# (scripts/e2e/acceptance.sh), so the dump holds every kind of row, a sealed marketplace token
# and the procrastinate queue. Without --seed-run on its own stack it seeds the demo profile only.
#
# Then: `pg_dump -Fc` of marketplace_os inside the source's compose postgres; a throwaway
# container from the compose postgres image (its own name, a loopback port Docker allocates,
# removed by the trap on success and failure); in it 0000_bootstrap.sql and the password file
# first (the roles are cluster-global and a dump does not carry them; the `bootstrap-mimic`
# pattern), then `pg_restore --exit-on-error`; then scripts/drill/check_restore.py against both
# (head, counts, procrastinate, forced RLS, CHECK helpers under search_path '', app_rw TCP login
# and tenant scoping, token_enc decrypt). The drill is timed from the dump: over 900 s fails.
# The dump lives in a 0700 temp dir and is removed with it. Loopback only. Local superuser for
# pg_restore; a non-superuser restore is S25 (T87). Not a required check.
set -euo pipefail
cd "$(dirname "$0")/../.."

stack=""
own_stack=0
seed_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --stack) stack="$2"; shift 2 ;;
    --seed-run) seed_run=1; shift ;;
    *) echo "usage: restore.sh [--stack STACK_DIR] [--seed-run]" >&2; exit 2 ;;
  esac
done
MAX_SECONDS="${DRILL_MAX_SECONDS:-900}"

work="$(mktemp -d "${TMPDIR:-/tmp}/mkos-drill.XXXXXX")"
chmod 0700 "$work"
container="mkos-drill-$$-$RANDOM"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$work"
  if [ "$own_stack" -eq 1 ] && [ -n "$stack" ]; then scripts/e2e/lib/fresh_stack.sh down "$stack" || true; fi
}
trap cleanup EXIT

if [ -z "$stack" ]; then
  own_stack=1
  stack="$(scripts/e2e/lib/fresh_stack.sh up --profile acceptance | sed -n 's/^STACK_DIR=//p' | tail -1)"
  [ -n "$stack" ] || { echo "drill: the fresh stack did not come up" >&2; exit 1; }
fi
[ -f "$stack/env" ] || { echo "drill: no stack at $stack" >&2; exit 2; }
set -a
# shellcheck disable=SC1091
. "$stack/env"
set +a
case "$base_url" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "drill: refusing a non-local base URL $base_url" >&2; exit 2 ;;
esac
case "$COMPOSE_PROJECT_NAME" in
  mkos-fresh-*) ;;
  *) echo "drill: refusing project $COMPOSE_PROJECT_NAME (not a fresh stack)" >&2; exit 2 ;;
esac

if [ "$own_stack" -eq 1 ]; then
  if [ "$seed_run" -eq 1 ]; then
    scripts/e2e/lib/acceptance_run.sh --stack "$stack" >&2  # scripts/e2e/acceptance.sh on it
  else
    APP_ROLE=migrate uv run --frozen python -m app.cli seed --profile demo >&2
  fi
fi

# the dev-only role passwords this stack's DSNs carry (.env; local constants, never real secrets)
dsn_password() {
  python3 -c 'import sys; from urllib.parse import unquote, urlsplit; print(unquote(urlsplit(sys.argv[1]).password or ""))' "$1"
}
umask 077
dsn_password "$DATABASE_URL" >"$work/app_rw.password"
dsn_password "$ALEMBIC_DATABASE_URL" >"$work/migrator.password"
image="$(uv run --frozen python -c 'import yaml; print(yaml.safe_load(open("docker-compose.yml"))["services"]["postgres"]["image"])')"

started="$(date +%s)"
echo "drill: pg_dump -Fc marketplace_os from $COMPOSE_PROJECT_NAME"
env COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT_NAME" PG_HOST_PORT="$PG_HOST_PORT" \
  MINIO_API_HOST_PORT="$MINIO_API_HOST_PORT" MINIO_CONSOLE_HOST_PORT="$MINIO_CONSOLE_HOST_PORT" \
  docker compose exec -T postgres pg_dump -U postgres -Fc marketplace_os >"$work/marketplace_os.dump"
[ -s "$work/marketplace_os.dump" ] || { echo "drill: empty dump" >&2; exit 1; }

echo "drill: throwaway container $container from the compose postgres image"
PGPASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
export PGPASSWORD
POSTGRES_PASSWORD="$PGPASSWORD" docker run -d --rm --name "$container" -p 127.0.0.1::5432 \
  -e POSTGRES_PASSWORD -e POSTGRES_DB=marketplace_os "$image" >/dev/null
waited=0
until docker exec "$container" pg_isready -h 127.0.0.1 -U postgres -d marketplace_os >/dev/null 2>&1; do
  waited=$((waited + 1))
  [ "$waited" -lt 60 ] || { echo "drill: the throwaway postgres never became ready" >&2; exit 1; }
  sleep 1
done
port="$(docker port "$container" 5432/tcp | head -n 1 | sed 's/.*://')"
psql_in() { docker exec -i "$@" "$container" psql -U postgres -d marketplace_os -v ON_ERROR_STOP=1 -X -q -f -; }

echo "drill: 0000_bootstrap.sql, then the password file (roles first: a dump carries none)"
psql_in <scripts/db/0000_bootstrap.sql
APP_RW_SCRAM="$(uv run --frozen python scripts/db/scram_verifier.py <"$work/app_rw.password")"
MIGRATOR_SCRAM="$(uv run --frozen python scripts/db/scram_verifier.py <"$work/migrator.password")"
export APP_RW_SCRAM MIGRATOR_SCRAM
psql_in -e APP_RW_SCRAM -e MIGRATOR_SCRAM <scripts/db/0000_role_passwords.sql
unset APP_RW_SCRAM MIGRATOR_SCRAM

echo "drill: pg_restore --exit-on-error"
docker exec -i "$container" pg_restore -U postgres -d marketplace_os --exit-on-error <"$work/marketplace_os.dump"

status=0
uv run --frozen python scripts/drill/check_restore.py \
  --source-dsn "postgresql://postgres:postgres@127.0.0.1:${PG_HOST_PORT}/marketplace_os" \
  --restored-dsn "postgresql://postgres@127.0.0.1:${port}/marketplace_os" \
  --app-rw-password-file "$work/app_rw.password" \
  --started "$started" --max-seconds "$MAX_SECONDS" || status=$?
echo "drill: elapsed $(( $(date +%s) - started )) s (max $MAX_SECONDS s), status=$status"
exit "$status"
