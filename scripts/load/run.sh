#!/usr/bin/env bash
# The v1 load smoke on a fresh stack (S26 T18; AC 7, DW2): `make load-smoke`.
#
#   scripts/load/run.sh [--out FILE]
#
# Brings up a fresh stack (scripts/e2e/lib/fresh_stack.sh, profile acceptance: its own compose
# project, ports, fake marketplace, API and one worker) with the committed production rate limits
# (no extra process env file: PLAN D2), seeds the local demo tenants (acme, bharat) and runs
# scripts/load/smoke.py with the stack's base URL and fake URL passed in. The stack is removed on
# success and failure. Loopback only. Not a required check.
set -euo pipefail
cd "$(dirname "$0")/../.."

out=".e2e/load-smoke.json"
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out="$2"; shift 2 ;;
    *) echo "usage: run.sh [--out FILE]" >&2; exit 2 ;;
  esac
done

stack=""
cleanup() {
  if [ -n "$stack" ]; then scripts/e2e/lib/fresh_stack.sh down "$stack" || true; fi
}
trap cleanup EXIT

stack="$(scripts/e2e/lib/fresh_stack.sh up --profile acceptance | sed -n 's/^STACK_DIR=//p' | tail -1)"
[ -n "$stack" ] || { echo "load-smoke: the fresh stack did not come up" >&2; exit 1; }
set -a
# shellcheck disable=SC1091
. "$stack/env"
set +a
case "$base_url" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "load-smoke: refusing a non-local base URL $base_url" >&2; exit 2 ;;
esac
APP_ROLE=migrate uv run --frozen python -m app.cli seed --profile demo >&2

started="$(date +%s)"
status=0
uv run --frozen python scripts/load/smoke.py --base-url "$base_url" --fake-url "$FAKE_URL" \
  --out "$out" || status=$?
echo "load-smoke: elapsed $(( $(date +%s) - started )) s, status=$status"
exit "$status"
