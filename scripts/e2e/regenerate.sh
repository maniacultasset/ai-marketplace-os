#!/usr/bin/env bash
# S18 regeneration, HTTP first (CODE-PLAN S18 T21; AC 17): an acme operator generates a listing
# with `stub:blocked`; after three correction attempts the listing is `blocked` /
# `corrections_exhausted` and the job envelope carries the blocking ErrorReport. A reviewer then
# regenerates it (202), the new run is `validated`, and one read-only `db_*` step checks that the
# new version names the reviewer as its maker (so lv_guard keeps that reviewer from approving it).
#
# Usage: WORKER_START=<cmd> WORKER_STOP=<cmd> scripts/e2e/regenerate.sh [base_url]
# No other worker may run against the stack (`make dev` starts none). Local only: the base URL must
# be on 127.0.0.1 or localhost, else exit 2 before any request. Every poll gives up after 60 s. On
# failure it prints the step name and the HTTP status, never a body.
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  echo "usage: WORKER_START=<cmd> WORKER_STOP=<cmd> $0 [base_url]" >&2
  exit 2
}
[ -n "${WORKER_START:-}" ] && [ -n "${WORKER_STOP:-}" ] || usage

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
api="$base/api/v1"
password="devpassword123" # the demo seed's local-only password (A§5)
run="$(date +%s)-$RANDOM"
category="ethnic_wear_kurta_sets" # the seeded category with a current Amazon template
POLL_LIMIT_S=60

work="$(mktemp -d)"
worker_running=0
cleanup() {
  if [ "$worker_running" -eq 1 ]; then bash -c "$WORKER_STOP" || true; fi
  rm -rf "$work"
}
trap cleanup EXIT
body="$work/body.json"
current="setup"
status=""

step() {
  current="$1"
  echo "e2e: step $current"
}

fail() {
  echo "e2e: FAILED at step $current (HTTP status ${status:-none}): $1" >&2
  exit 1
}

expect() {
  if [ "$status" != "$1" ]; then fail "want HTTP $1"; fi
}

# call METHOD PATH [TOKEN] [KEY] [JSON]: the status goes in $status, the body in $body.
call() {
  local method="$1" path="$2" token="${3:-}" key="${4:-}" data="${5:-}"
  local args=(-s -o "$body" -w '%{http_code}' -X "$method" -H 'Accept: application/json')
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$key" ]; then args+=(-H "Idempotency-Key: $key"); fi
  if [ -n "$data" ]; then args+=(-H 'Content-Type: application/json' --data-binary "$data"); fi
  status="$(curl "${args[@]}" "$api$path")" || status="000"
}

login() {
  call POST /auth/login "" "" "{\"email\": \"$1\", \"password\": \"$password\"}"
  expect 200
  json 'b["access_token"]'
}

# json EXPR: evaluate a Python expression over the parsed body `b`; prints the result.
json() {
  python3 - "$body" "$1" <<'PY' || fail "unexpected response shape"
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        b = json.load(fh)
    scope = {"__builtins__": {}, "b": b, "len": len, "str": str, "all": all, "set": set}
    out = eval(sys.argv[2], scope)  # noqa: S307 - the script's own fixed expressions
except Exception:  # no traceback: it could quote the response
    raise SystemExit(1) from None
if isinstance(out, bool):
    if not out:
        raise SystemExit(1)
    print("true")
elif out is None:
    print("")
else:
    print(out)
PY
}

# wait_job TOKEN JOB STATUS: poll the job envelope until STATUS, at most POLL_LIMIT_S seconds.
wait_job() {
  local deadline=$(( $(date +%s) + POLL_LIMIT_S )) seen
  while :; do
    call GET "/jobs/$2" "$1"
    expect 200
    seen="$(json 'b["status"]')"
    if [ "$seen" = "$3" ]; then return 0; fi
    case "$seen" in succeeded|partial|failed|cancelled|dead_letter) fail "the job ended $seen, want $3" ;; esac
    if [ "$(date +%s)" -ge "$deadline" ]; then fail "the job did not reach $3 within ${POLL_LIMIT_S} s"; fi
    sleep 1
  done
}

# sql QUERY: one read-only value from the local stack's database (the db_* steps only).
sql() {
  docker compose exec -T postgres psql -U postgres -d marketplace_os -v ON_ERROR_STOP=1 -qtA \
    -c "SET default_transaction_read_only = on" -c "$1"
}

new_product() {
  call POST /products "$operator" "" "{\"sku\": \"$1\", \"title\": \"E2E Generated Kurta Set\", \"category\": \"$category\"}"
  expect 201
  json 'b["id"]'
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step login_acme_operator
operator="$(login operator@acme.test)"

step create_product_201
product="$(new_product "E2E-GEN-$(date +%s)-$RANDOM")"

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

step generate_stub_blocked_202
call POST /listings/generate "$operator" "e2e-regen-$run" \
  "{\"product_id\": \"$product\", \"marketplace\": \"amazon\", \"user_instructions\": \"stub:blocked\"}"
expect 202
job_id="$(json 'b["job_id"]')"
listing_id="$(json 'b["listing_id"]')"

step poll_blocked
wait_job "$operator" "$job_id" succeeded
call GET "/listings/$listing_id/status" "$operator"
expect 200
json 'b["status"] == "blocked" and b["status_reason"] == "corrections_exhausted"' >/dev/null

step job_error_present
call GET "/jobs/$job_id" "$operator"
expect 200
json 'b["error"] is not None and len(b["error"]["errors"]) >= 1' >/dev/null

step login_acme_reviewer
reviewer="$(login reviewer@acme.test)"

step reviewer_regenerate_202
call POST "/listings/$listing_id/regenerate" "$reviewer" "e2e-regen-reviewer-$run" \
  '{"instructions": "a shorter title"}'
expect 202
regen_job="$(json 'b["job_id"]')"
json 'b["listing_id"] == "'"$listing_id"'"' >/dev/null

step poll_validated
wait_job "$reviewer" "$regen_job" succeeded
call GET "/listings/$listing_id/status" "$reviewer"
expect 200
json 'b["status"] == "validated"' >/dev/null

step db_maker_is_reviewer
[ "$(sql "SELECT u.email FROM listings l JOIN listing_versions v ON v.id = l.current_version_id JOIN users u ON u.id = v.generated_by_user_id WHERE l.id = '$listing_id' AND v.origin = 'regeneration'")" = "reviewer@acme.test" ] \
  || fail "want the reviewer as the new version's maker"

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

echo "e2e: regenerate OK (listing $listing_id)"
