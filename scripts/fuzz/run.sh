#!/usr/bin/env bash
# Live Schemathesis over the whole served OpenAPI (S26 T15; AC 4): `make fuzz`.
#
#   scripts/fuzz/run.sh [--max-examples N] [--seed S]
#
# Brings up a fresh stack (scripts/e2e/lib/fresh_stack.sh, profile acceptance) with raised rate
# limits in the API's process env only (PLAN D6: the committed defaults never change), seeds the
# local demo tenants, logs in one user per role over HTTP and runs `schemathesis run` once per
# token: every check but the two below, every phase (stateful included), the given seed.
# Excluded (S26 DEVIATIONS): `positive_data_acceptance` (the API's validators are stricter than
# the OpenAPI can say: control characters, cross-field rules, cursor tokens; a contract change is
# out of S26's scope) and `negative_data_rejection` (unknown query parameters are ignored, by
# design, and the request models strip surrounding whitespace before the length check). Every
# 5xx (`not_a_server_error`), `unsupported_method` and every other check fails the run.
# Operator and reviewer go first; admin and owner last, because a stateful PATCH /members or
# tenant change can demote or revoke a token; a token that answers 401 is logged in again.
# Prints the seed; exits non-zero on any failure. Loopback only. Not a required check.
set -euo pipefail
cd "$(dirname "$0")/../.."

max_examples=50
seed="${FUZZ_SEED:-$RANDOM$RANDOM}"
while [ $# -gt 0 ]; do
  case "$1" in
    --max-examples) max_examples="$2"; shift 2 ;;
    --seed) seed="$2"; shift 2 ;;
    *) echo "usage: run.sh [--max-examples N] [--seed S]" >&2; exit 2 ;;
  esac
done
ROLES="operator reviewer admin owner"
PASSWORD="devpassword123"  # the local-only demo seed password (app/db/seed_ops.py)

limits="$(mktemp)"
cat >"$limits" <<'LIMITS'
RATE_LIMIT_PER_TENANT_PER_MIN=100000
RATE_LIMIT_PER_IP_PER_MIN=100000
AUTH_LOGIN_RATE_LIMIT_PER_MIN=100000
LIMITS
stack=""
cleanup() {
  rm -f "$limits"
  if [ -n "$stack" ]; then scripts/e2e/lib/fresh_stack.sh down "$stack" || true; fi
}
trap cleanup EXIT

stack="$(FRESH_ENV_FILE="$limits" scripts/e2e/lib/fresh_stack.sh up --profile acceptance | sed -n 's/^STACK_DIR=//p' | tail -1)"
[ -n "$stack" ] || { echo "fuzz: the fresh stack did not come up" >&2; exit 1; }
set -a
# shellcheck disable=SC1091
. "$stack/env"
set +a
case "$base_url" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "fuzz: refusing a non-local base URL $base_url" >&2; exit 2 ;;
esac
APP_ROLE=migrate uv run --frozen python -m app.cli seed --profile demo >&2

login() {
  curl -sf -X POST "$base_url/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\": \"$1@acme.test\", \"password\": \"$PASSWORD\"}" |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
}

echo "fuzz: seed=$seed max_examples=$max_examples base=$base_url"
status=0
for role in $ROLES; do
  token="$(login "$role")"
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" "$base_url/api/v1/auth/me")"
  if [ "$code" = 401 ]; then token="$(login "$role")"; fi
  echo "fuzz: role=$role"
  uv run --frozen schemathesis run "$base_url/openapi.json" \
    --checks all \
    --exclude-checks positive_data_acceptance,negative_data_rejection \
    --phases examples,coverage,fuzzing,stateful \
    --seed "$seed" \
    --max-examples "$max_examples" \
    --header "Authorization: Bearer $token" \
    --include-path-regex '^/api/v1/' \
    --request-timeout 30 \
    --no-color || status=1
done
echo "fuzz: seed=$seed status=$status"
exit "$status"
