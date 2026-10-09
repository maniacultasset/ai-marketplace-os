#!/usr/bin/env bash
# S19 review and approval, HTTP only (CODE-PLAN S19 T15; AC 16): an acme owner generates a listing
# (`validated`); the owner's own approve is an item 403 MAKER_CHECKER_VIOLATION (the generator
# never approves), an operator may not approve at all (403 PERMISSION_DENIED), and a second user,
# the reviewer, approves it (item 200, `approved`). The same Idempotency-Key replays the same
# body; nothing is sent to a marketplace (`not_submitted`); the history shows the denial and the
# approval; bharat reads acme's listing as 404.
#
# Usage: WORKER_START=<cmd> WORKER_STOP=<cmd> scripts/e2e/review_approve.sh [base_url]
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
head="$work/head.txt"
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

# call METHOD PATH [TOKEN] [KEY] [JSON] [IF_MATCH]: the status goes in $status, the body in $body.
call() {
  local method="$1" path="$2" token="${3:-}" key="${4:-}" data="${5:-}" match="${6:-}"
  local args=(-s -o "$body" -D "$head" -w '%{http_code}' -X "$method" -H 'Accept: application/json')
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$key" ]; then args+=(-H "Idempotency-Key: $key"); fi
  if [ -n "$match" ]; then args+=(-H "If-Match: \"$match\""); fi
  if [ -n "$data" ]; then args+=(-H 'Content-Type: application/json' --data-binary "$data"); fi
  status="$(curl "${args[@]}" "$api$path")" || status="000"
}

login() {
  call POST /auth/login "" "" "{\"email\": \"$1\", \"password\": \"$password\"}"
  expect 200
  json 'b["access_token"]'
}

# etag: the ETag header of the last response, quotes stripped (empty when absent).
etag() {
  tr -d '\r' <"$head" | awk -F': ' 'tolower($1) == "etag" { gsub(/"/, "", $2); print $2 }' | tail -n 1
}

# approve TOKEN KEY ITEMS_JSON: POST /listings/approve; the one item's result in $item_status.
approve() {
  call POST /listings/approve "$1" "$2" "{\"items\": [$3]}"
  expect 200
  item_status="$(json 'b["items"][0]["http_status"]')"
  item_code="$(json '(b["items"][0]["error"] or {}).get("error_code", "")')"
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

new_product() {
  call POST /products "$1" "" "{\"sku\": \"$2\", \"title\": \"E2E Review Kurta Set\", \"category\": \"$category\"}"
  expect 201
  json 'b["id"]'
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step login_acme_owner
owner="$(login owner@acme.test)"

step create_product_201
product="$(new_product "$owner" "E2E-GEN-$(date +%s)-$RANDOM")"

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

step owner_generate_202
call POST /listings/generate "$owner" "e2e-review-gen-$run" "{\"product_id\": \"$product\", \"marketplace\": \"amazon\"}"
expect 202
job_id="$(json 'b["job_id"]')"
listing_id="$(json 'b["listing_id"]')"

step poll_validated
wait_job "$owner" "$job_id" succeeded
call GET "/listings/$listing_id" "$owner"
expect 200
json 'b["status"] == "validated"' >/dev/null
version="$(json 'b["current_version_id"]')"
[ "$(etag)" = "$version" ] || fail "the ETag is not the current version"
decision="{\"listing_id\": \"$listing_id\", \"listing_version_id\": \"$version\", \"decision\": \"approve\"}"

step owner_approve_maker_checker_403
approve "$owner" "e2e-review-owner-$run" "$decision"
[ "$item_status" = "403" ] && [ "$item_code" = "MAKER_CHECKER_VIOLATION" ] || fail "the generator approved (item $item_status $item_code)"

step login_acme_operator
operator="$(login operator@acme.test)"

step operator_approve_permission_403
call POST /listings/approve "$operator" "e2e-review-op-$run" "{\"items\": [$decision]}"
expect 403
json 'b["error"]["type"] == "PERMISSION_DENIED"' >/dev/null

step login_acme_reviewer
reviewer="$(login reviewer@acme.test)"

step reviewer_approve_200
approve "$reviewer" "e2e-review-ok-$run" "$decision"
[ "$item_status" = "200" ] || fail "the reviewer's approve was item $item_status $item_code"
json 'b["items"][0]["listing_status"] == "approved"' >/dev/null
cp "$body" "$work/first.json"

step replay_same_key_identical
approve "$reviewer" "e2e-review-ok-$run" "$decision"
cmp -s "$body" "$work/first.json" || fail "the replay answered another body"

step not_submitted
call GET "/listings/$listing_id" "$reviewer"
expect 200
json 'b["status"] == "approved" and b["marketplace_state"] == "not_submitted" and b["approved_version_id"] == b["current_version_id"]' >/dev/null

step history_denied_and_approved
call GET "/listings/$listing_id/history" "$reviewer"
expect 200
json '"listing.approve_denied" in [e["action"] for e in b["data"]] and "listing.approved" in [e["action"] for e in b["data"]]' >/dev/null

step review_queue_lists_validated
call GET "/listings?status=validated" "$reviewer"
expect 200
json 'all(r["status"] == "validated" for r in b["data"])' >/dev/null

step login_bharat_reviewer
bharat="$(login reviewer@bharat.test)"

step bharat_get_404
call GET "/listings/$listing_id" "$bharat"
expect 404

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

echo "e2e: review_approve OK (listing $listing_id)"
