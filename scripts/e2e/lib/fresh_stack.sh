#!/usr/bin/env bash
# A fresh local stack for the S26 harnesses (fuzz, acceptance, load, drill, e2e-fresh): its own
# compose project and its own loopback ports, never this checkout's (S26 T17).
#
# Usage:
#   scripts/e2e/lib/fresh_stack.sh up --profile acceptance|e2e   build it; prints STACK_DIR=<dir>
#   scripts/e2e/lib/fresh_stack.sh env [STACK_DIR]               print the stack's env file
#   scripts/e2e/lib/fresh_stack.sh down [STACK_DIR]              stop it, remove it, every time
# STACK_DIR defaults to $FRESH_STACK_DIR. FRESH_ENV_FILE names extra KEY=VALUE lines for the API
# and worker process env only (the fuzz run's raised limits); committed defaults never change.
#
# Isolation: `worktree_env.sh` never picks a port and the Makefile includes `.worktree.env`, so
# every `make` call here gets the stack's project and ports as command-line overrides (which beat
# the included file) and every docker compose call gets them in its own environment; the scripts
# a caller runs get `base_url` from the env file, never from this checkout's `.worktree.env`.
# `up` refuses (exit 2) a project name or port equal to this checkout's or a Makefile default.
#
# Profiles (both written to $STACK_DIR/env):
#   acceptance  `platform load` (never `seed`), FLIPKART_ENABLED=false, its own fake marketplace,
#               the API and one worker on this stack; marketplace URLs point at the fake.
#   e2e         `make seed`, FLIPKART_ENABLED=true; no fake and no worker (`make e2e` starts its
#               own fake; the scripts' WORKER_START/WORKER_STOP hooks own every worker, and the
#               hooks in the env file carry this stack's overrides).
set -euo pipefail
cd "$(dirname "$0")/../../.."

WAIT_CAP="${FRESH_STACK_WAIT_SECONDS:-60}"
DEFAULT_PORTS="5432 9000 9001 8000 4010 4020"
PORT_KEYS="PG_HOST_PORT MINIO_API_HOST_PORT MINIO_CONSOLE_HOST_PORT API_HOST_PORT FAKE_MARKETPLACE_HOST_PORT FLAG_OFF_API_HOST_PORT"
COMPOSE_KEYS="COMPOSE_PROJECT_NAME PG_HOST_PORT MINIO_API_HOST_PORT MINIO_CONSOLE_HOST_PORT"
FAKE_APP_ID="amzn1.sp.solution.REPLACE_ME"

FRESH_OK=0

log() { echo "fresh_stack: $*" >&2; }
die() { log "$1"; exit "${2:-1}"; }

stack_dir() { printf '%s' "${1:-${FRESH_STACK_DIR:-}}"; }

# The stack's own env, as `export K=V` lines for a subshell.
load_env() {
  local dir="$1"
  [ -f "$dir/env" ] || die "no stack at $dir (no env file)"
  set -a
  # shellcheck disable=SC1091
  . "$dir/env"
  set +a
}

# free loopback ports: python3 binds 127.0.0.1:0 for each key and reads the port back.
allocate() {
  # shellcheck disable=SC2086
  python3 - $PORT_KEYS <<'PY'
import socket
import sys

held, out = [], []
for key in sys.argv[1:]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    held.append(s)
    out.append(f"{key}={s.getsockname()[1]}")
for s in held:
    s.close()
print("\n".join(out))
PY
}

# Exit 2 when the project or a port equals this checkout's (.worktree.env) or a default.
refuse_clashes() {
  local project="$1" assignments="$2" mine="" key port
  if [ -f .worktree.env ]; then
    mine="$(scripts/ci/worktree_env.sh exports .worktree.env)" || die "bad .worktree.env"
  fi
  if printf '%s\n' "$mine" | grep -qx "export COMPOSE_PROJECT_NAME=$project"; then
    die "project $project is this checkout's" 2
  fi
  case "$project" in mkos-fresh-*) ;; *) die "project $project is not a fresh-stack name" 2 ;; esac
  while IFS='=' read -r key port; do
    [ -n "$key" ] || continue
    if printf '%s\n' "$mine" | grep -qE "^export [A-Z_]+=$port\$"; then
      die "$key $port clashes with this checkout's .worktree.env" 2
    fi
    case " $DEFAULT_PORTS " in *" $port "*) die "$key $port is a Makefile default port" 2 ;; esac
  done <<<"$assignments"
}

# `make` on this stack: every port and the project as command-line overrides, and in the env.
stack_make() {
  local overrides=() key
  for key in $COMPOSE_KEYS API_HOST_PORT FAKE_MARKETPLACE_HOST_PORT; do
    overrides+=("$key=${!key}")
  done
  env "${overrides[@]}" make --no-print-directory "${overrides[@]}" "$@"
}

stack_compose() {
  env COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT_NAME" PG_HOST_PORT="$PG_HOST_PORT" \
    MINIO_API_HOST_PORT="$MINIO_API_HOST_PORT" MINIO_CONSOLE_HOST_PORT="$MINIO_CONSOLE_HOST_PORT" \
    docker compose "$@"
}

# start NAME CMD...: in its own process group (so `down` kills its children), logs under logs/.
start() {
  local name="$1"
  shift
  python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@" \
    >"$STACK_DIR/logs/$name.log" 2>&1 </dev/null &
  echo "$!" >>"$STACK_DIR/pids"
  log "started $name (pid $!)"
}

# wait_http URL: 1 s polls, at most WAIT_CAP seconds.
wait_http() {
  local url="$1" waited=0
  until curl -sf -o /dev/null "$url"; do
    waited=$((waited + 1))
    [ "$waited" -lt "$WAIT_CAP" ] || die "$url never answered in ${WAIT_CAP}s"
    sleep 1
  done
}

has_setting() {
  uv run --frozen python -c "import sys; from app.core.config import Settings; sys.exit(0 if '$1' in Settings.model_fields else 1)" 2>/dev/null
}

down() {
  local dir="$1" pid
  [ -n "$dir" ] && [ -d "$dir" ] || { log "nothing to stop"; return 0; }
  if [ -f "$dir/pids" ]; then
    while read -r pid; do
      [ -n "$pid" ] || continue
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done <"$dir/pids"
    sleep 1
    while read -r pid; do
      [ -n "$pid" ] || continue
      kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    done <"$dir/pids"
  fi
  if [ -f "$dir/env" ]; then
    (load_env "$dir" && stack_compose down -v --remove-orphans >/dev/null 2>&1) || true
  fi
  rm -rf "$dir"
  log "down"
}

up() {
  local profile=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --profile) profile="${2:-}"; shift 2 ;;
      *) die "unknown argument $1" 2 ;;
    esac
  done
  case "$profile" in acceptance|e2e) ;; *) die "--profile acceptance|e2e is required" 2 ;; esac

  COMPOSE_PROJECT_NAME="${FRESH_PROJECT_NAME:-mkos-fresh-$$-$RANDOM}"
  local assignments
  assignments="$(allocate)"
  if [ -n "${FRESH_FORCE_PORTS:-}" ]; then  # tests only: KEY=PORT,... replaces an allocation
    local forced key
    for forced in ${FRESH_FORCE_PORTS//,/ }; do
      key="${forced%%=*}"
      assignments="$(printf '%s\n' "$assignments" | sed "s/^${key}=.*/${forced}/")"
    done
  fi
  refuse_clashes "$COMPOSE_PROJECT_NAME" "$assignments"
  [ -f .env ] || die "no .env (cp .env.example .env)"

  STACK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mkos-fresh.XXXXXX")"
  chmod 0700 "$STACK_DIR"
  mkdir -p "$STACK_DIR/logs"
  : >"$STACK_DIR/pids"
  # shellcheck disable=SC2064 # the dir is fixed now
  trap "[ \"\$FRESH_OK\" -eq 1 ] || down '$STACK_DIR'" EXIT
  eval "$(printf '%s\n' "$assignments" | sed 's/^/export /')"
  export COMPOSE_PROJECT_NAME

  local app_dsn migrator_dsn s3 fake base flipkart
  app_dsn="$(sed -n 's/^DATABASE_URL=//p' .env | sed -E "s#@localhost:[0-9]+/#@localhost:${PG_HOST_PORT}/#")"
  migrator_dsn="$(sed -n 's/^ALEMBIC_DATABASE_URL=//p' .env | sed -E "s#@localhost:[0-9]+/#@localhost:${PG_HOST_PORT}/#")"
  s3="http://localhost:${MINIO_API_HOST_PORT}"
  fake="http://127.0.0.1:${FAKE_MARKETPLACE_HOST_PORT}"
  base="http://127.0.0.1:${API_HOST_PORT}"
  flipkart=false
  [ "$profile" = e2e ] && flipkart=true

  {
    echo "STACK_DIR=$STACK_DIR"
    echo "PROFILE=$profile"
    for key in $COMPOSE_KEYS API_HOST_PORT FAKE_MARKETPLACE_HOST_PORT FLAG_OFF_API_HOST_PORT; do
      echo "$key=${!key}"
    done
    echo "base_url=$base"
    echo "FAKE_URL=$fake"
    echo "DATABASE_URL=$app_dsn"
    echo "ALEMBIC_DATABASE_URL=$migrator_dsn"
    echo "S3_ENDPOINT_URL=$s3"
    echo "PORT=$API_HOST_PORT"
    if has_setting flipkart_enabled; then echo "FLIPKART_ENABLED=$flipkart"; fi
    echo "AMAZON_APP_ID=$FAKE_APP_ID"
    echo "AMAZON_AUTHORIZE_URL=$fake/amazon/authorize"
    echo "AMAZON_LWA_TOKEN_URL=$fake/amazon/token"
    echo "FLIPKART_AUTHORIZE_URL=$fake/flipkart/authorize"
    echo "FLIPKART_TOKEN_URL=$fake/flipkart/token"
    echo "AMAZON_SPAPI_ENDPOINT=$fake"
    if has_setting flipkart_api_base; then echo "FLIPKART_API_BASE=$fake/flipkart/sellers"; fi  # the fake's FK_BASE, as `make dev`
    if [ "$profile" = e2e ]; then
      local overrides="COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME PG_HOST_PORT=$PG_HOST_PORT API_HOST_PORT=$API_HOST_PORT"
      echo "WORKER_START='mkdir -p $STACK_DIR/e2e; env $overrides DATABASE_URL=$app_dsn ALEMBIC_DATABASE_URL=$migrator_dsn S3_ENDPOINT_URL=$s3 APP_ROLE=worker APP_NAME=marketplace-os-worker OTEL_SERVICE_NAME=marketplace-os-worker nohup uv run --frozen python -m app.workers.main >$STACK_DIR/logs/worker.log 2>&1 & echo \$! >$STACK_DIR/e2e/worker.pid'"
      echo "WORKER_STOP='pid=\$(cat $STACK_DIR/e2e/worker.pid); kill -TERM \$pid; for _ in \$(seq 1 30); do kill -0 \$pid 2>/dev/null || break; sleep 1; done; ! kill -0 \$pid 2>/dev/null && rm -f $STACK_DIR/e2e/worker.pid'"
      echo "TEMPLATES_LOAD='env ALEMBIC_DATABASE_URL=$migrator_dsn uv run --frozen python -m app.cli templates load --file \"\$1\"'"
      echo "ERRORS_LOAD='env ALEMBIC_DATABASE_URL=$migrator_dsn uv run --frozen python -m app.cli errors load --file \"\$1\"'"
      echo "MKOS_CLI='env ALEMBIC_DATABASE_URL=$migrator_dsn DATABASE_URL=$app_dsn uv run --frozen python -m app.cli'"
    fi
  } >"$STACK_DIR/env"
  if [ -n "${FRESH_ENV_FILE:-}" ]; then
    grep -E '^[A-Z][A-Z0-9_]*=' "$FRESH_ENV_FILE" >"$STACK_DIR/process.env" || true
  else
    : >"$STACK_DIR/process.env"
  fi
  log "project $COMPOSE_PROJECT_NAME, api $base, profile $profile"

  stack_compose up -d --wait postgres minio >&2
  stack_compose run --rm minio-init >&2
  stack_make bootstrap-db >&2
  (load_env "$STACK_DIR" && uv run --frozen alembic -c apps/api/alembic.ini upgrade head) >&2
  (load_env "$STACK_DIR" && APP_ROLE=migrate uv run --frozen python -m app.cli dictionary sync) >&2
  if [ "$profile" = acceptance ]; then
    (load_env "$STACK_DIR" && APP_ROLE=migrate uv run --frozen python -m app.cli platform load) >&2
    start fake uv run --frozen python apps/api/tests/support/fake_marketplace.py \
      --port "$FAKE_MARKETPLACE_HOST_PORT"
    wait_http "$fake/healthz"
  else
    (load_env "$STACK_DIR" && stack_make seed) >&2
  fi
  # the API (127.0.0.1, uvicorn's default host) and, for acceptance only, one worker
  local proc_env=(env)
  while IFS= read -r line; do proc_env+=("$line"); done < <(grep -v '^$' "$STACK_DIR/process.env" || true)
  # shellcheck disable=SC1091
  (load_env "$STACK_DIR" && start api "${proc_env[@]}" uv run --frozen uvicorn app.main:app \
    --port "$API_HOST_PORT" --app-dir apps/api)
  if [ "$profile" = acceptance ]; then
    (load_env "$STACK_DIR" && start worker "${proc_env[@]}" APP_ROLE=worker \
      APP_NAME=marketplace-os-worker OTEL_SERVICE_NAME=marketplace-os-worker \
      uv run --frozen python -m app.workers.main)
  fi
  wait_http "$base/healthz"
  wait_http "$base/readyz"
  FRESH_OK=1
  echo "STACK_DIR=$STACK_DIR"
}

case "${1:-}" in
  up) shift; up "$@" ;;
  env) dir="$(stack_dir "${2:-}")"; [ -n "$dir" ] || die "STACK_DIR?" 2; cat "$dir/env" ;;
  down) down "$(stack_dir "${2:-}")" ;;
  *) echo "usage: fresh_stack.sh up --profile acceptance|e2e | env [DIR] | down [DIR]" >&2; exit 2 ;;
esac
