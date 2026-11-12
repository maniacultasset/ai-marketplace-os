#!/usr/bin/env bash
# SI4 phase A, HTTP only (CODE-PLAN A19; AC5, AC6): an acme operator generates with
# `stub:long_title` (a 212-character title, over the seeded 200-character Amazon rule); the
# backend's deterministic fix pass shortens it at a word boundary, so the listing is `validated`
# with one rules call. `/issues?status=auto_fixed` lists the fixed item with its `action_taken`,
# the listing history shows `listing.auto_fixed`, and bharat's inbox lacks it.
# The missing-fact leg is not here: the demo seed has no rule the stub leaves unfilled, so no
# HTTP-only path opens a fact question; the integration suite (test_issue_tasks.py) proves it.
#
# Usage: WORKER_START=<cmd> WORKER_STOP=<cmd> scripts/e2e/auto_fix.sh [base_url]
# No other worker may run against the stack (`make dev` starts none). Local only: the base URL must
# be on 127.0.0.1 or localhost, else exit 2 before any request. Every poll gives up after 60 s. On
# failure it prints the step name and the HTTP status, never a body.
set -euo pipefail
cd "$(dirname "$0")/../.."
# shellcheck source=scripts/e2e/lib/product_facts.sh
. scripts/e2e/lib/product_facts.sh

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
  call POST /products "$1" "" "{\"sku\": \"$2\", \"title\": \"E2E Auto Fix Kurta Set\", \"category\": \"$category\", $PRODUCT_FACTS}"
  expect 201
  json 'b["id"]'
}

# generate PRODUCT SCENARIO KEY: start a generation; sets $job_id and $listing_id.
generate() {
  call POST /listings/generate "$operator" "$3" "{\"product_id\": \"$1\", \"marketplace\": \"amazon\", \"user_instructions\": \"stub:$2\"}"
  expect 202
  job_id="$(json 'b["job_id"]')"
  listing_id="$(json 'b["listing_id"]')"
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step login_acme_operator
operator="$(login operator@acme.test)"

step create_product_201
product="$(new_product "$operator" "E2E-FIX-$(date +%s)-$RANDOM")"

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

step generate_long_title_202
generate "$product" long_title "e2e-autofix-gen-$run"
listing="$listing_id"

step poll_validated
wait_job "$operator" "$job_id" succeeded
call GET "/listings/$listing/status" "$operator"
expect 200
json 'b["status"] == "validated"' >/dev/null

step title_within_limit
call GET "/listings/$listing" "$operator"
expect 200
json 'len(b["content"]["content"]["title"]) <= 200' >/dev/null

step issues_auto_fixed_one
call GET "/issues?status=auto_fixed&listing_id=$listing" "$operator"
expect 200
json 'len(b["data"]) == 1 and b["data"][0]["resolution"] == "deterministic_fix"' >/dev/null
json 'len(b["data"][0]["action_taken"] or "") > 0 and b["data"][0]["fix_action"] == "reformat"' >/dev/null
fixed="$(json 'b["data"][0]["id"]')"

step history_has_auto_fixed
call GET "/listings/$listing/history" "$operator"
expect 200
json '"listing.auto_fixed" in [h["action"] for h in b["data"]]' >/dev/null

step bharat_sees_none
bharat="$(login operator@bharat.test)"
call GET "/issues?status=auto_fixed" "$bharat"
expect 200
json '"'"$fixed"'" not in [r["id"] for r in b["data"]]' >/dev/null

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

echo "e2e: auto_fix OK (listing $listing)"
