#!/usr/bin/env bash
# `make acceptance` (S26 T20-T21; AC 9): the v1 acceptance run on a fresh stack.
#
#   scripts/e2e/lib/acceptance_run.sh [--stack STACK_DIR]
#
# Without --stack it brings up its own fresh stack (scripts/e2e/lib/fresh_stack.sh, profile
# acceptance: its own compose project and ports, its own fake marketplace, the API and one worker,
# FLIPKART_ENABLED=false) and removes it afterwards, on success and failure. It runs
# scripts/e2e/acceptance.sh with the stack's base URL, its fake's URL, the staff CLI bound to the
# stack's database (never this checkout's .worktree.env or .env DSNs) and the stack's log dir.
set -euo pipefail
cd "$(dirname "$0")/../../.."

stack=""
own=1
while [ $# -gt 0 ]; do
  case "$1" in
    --stack) stack="$2"; own=0; shift 2 ;;
    *) echo "usage: acceptance_run.sh [--stack STACK_DIR]" >&2; exit 2 ;;
  esac
done
cleanup() {
  if [ "$own" -eq 1 ] && [ -n "$stack" ]; then scripts/e2e/lib/fresh_stack.sh down "$stack" || true; fi
}
trap cleanup EXIT

if [ -z "$stack" ]; then
  stack="$(scripts/e2e/lib/fresh_stack.sh up --profile acceptance | sed -n 's/^STACK_DIR=//p' | tail -1)"
  [ -n "$stack" ] || { echo "acceptance: the fresh stack did not come up" >&2; exit 1; }
fi
[ -f "$stack/env" ] || { echo "acceptance: no stack at $stack" >&2; exit 2; }
set -a
# shellcheck disable=SC1091
. "$stack/env"
set +a
[ "$PROFILE" = acceptance ] || { echo "acceptance: the stack's profile is $PROFILE" >&2; exit 2; }
[ "${FLIPKART_ENABLED:-false}" = false ] || { echo "acceptance: FLIPKART_ENABLED must be false" >&2; exit 2; }
export FAKE_URL STACK_LOGS="$stack/logs"
export MKOS_CLI="env ALEMBIC_DATABASE_URL=$ALEMBIC_DATABASE_URL DATABASE_URL=$DATABASE_URL uv run --frozen python -m app.cli"
started="$(date +%s)"
status=0
scripts/e2e/acceptance.sh "$base_url" || status=$?
echo "acceptance: elapsed $(( $(date +%s) - started )) s, status=$status"
exit "$status"
