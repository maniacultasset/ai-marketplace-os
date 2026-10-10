#!/usr/bin/env bash
# `make e2e-fresh` (S26 T22; AC 10): every scripts/e2e/*.sh, twice, on a fresh stack.
#
# Brings up scripts/e2e/lib/fresh_stack.sh profile e2e (its own compose project and ports, `make
# seed` done, the API with FLIPKART_ENABLED=true, no worker and no fake: `make e2e` starts its own
# fake and flag-off API, and the scripts' hooks own every worker), then runs `make e2e` twice
# against it and removes the stack, on success and failure. Isolation from this checkout:
# - every port, the project and the stack's hooks (WORKER_START/WORKER_STOP, TEMPLATES_LOAD,
#   ERRORS_LOAD, MKOS_CLI, from the stack's env file) go to `make` as command-line overrides,
#   which beat the included .worktree.env;
# - the stack's DSNs and S3 endpoint are in the environment of the make call (the env beats
#   .env), so the one-shot refresh and the flag-off API reach the stack's database;
# - FRESH_STACK_WORKTREE_ENV names the stack's own worktree-format file, which
#   scripts/ci/worktree_env.sh `exports` reads instead of .worktree.env, so each script, its
#   read-only `db_*` steps and S12's limiter reset before every script reach this stack's
#   postgres (`docker compose exec` under the stack's COMPOSE_PROJECT_NAME), never this checkout's.
set -euo pipefail
cd "$(dirname "$0")/../../.."

PASSES="${E2E_FRESH_PASSES:-2}"
KEYS="COMPOSE_PROJECT_NAME PG_HOST_PORT MINIO_API_HOST_PORT MINIO_CONSOLE_HOST_PORT API_HOST_PORT FAKE_MARKETPLACE_HOST_PORT FLAG_OFF_API_HOST_PORT"
HOOKS="WORKER_START WORKER_STOP TEMPLATES_LOAD ERRORS_LOAD MKOS_CLI"

stack=""
cleanup() {
  if [ -n "$stack" ]; then scripts/e2e/lib/fresh_stack.sh down "$stack" || true; fi
}
trap cleanup EXIT

stack="$(scripts/e2e/lib/fresh_stack.sh up --profile e2e | sed -n 's/^STACK_DIR=//p' | tail -1)"
[ -n "$stack" ] || { echo "e2e-fresh: the fresh stack did not come up" >&2; exit 1; }
set -a
# shellcheck disable=SC1091
. "$stack/env"
set +a
[ "$PROFILE" = e2e ] || { echo "e2e-fresh: the stack's profile is $PROFILE" >&2; exit 2; }
case "$COMPOSE_PROJECT_NAME" in
  mkos-fresh-*) ;;
  *) echo "e2e-fresh: refusing project $COMPOSE_PROJECT_NAME" >&2; exit 2 ;;
esac

overrides=()
: >"$stack/worktree.env"
for key in $KEYS; do
  overrides+=("$key=${!key}")
  echo "$key=${!key}" >>"$stack/worktree.env"
done
for hook in $HOOKS; do
  value="${!hook}"
  overrides+=("E2E_$hook=${value//\$/\$\$}")  # make expands `$`; the hook keeps its own
done
export FRESH_STACK_WORKTREE_ENV="$stack/worktree.env"

status=0
for pass in $(seq 1 "$PASSES"); do
  echo "e2e-fresh: pass $pass of $PASSES on $COMPOSE_PROJECT_NAME ($base_url)"
  make --no-print-directory "${overrides[@]}" e2e || { status=$?; break; }
done
echo "e2e-fresh: status=$status"
exit "$status"
