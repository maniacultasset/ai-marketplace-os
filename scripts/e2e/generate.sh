#!/usr/bin/env bash
# S18 listing generation, HTTP first (CODE-PLAN S18 T21; AC 17): an acme operator creates a fresh
# product and asks for an Amazon listing (202); a replay of the key returns the same 202 and a new
# key while it is `generating` is 409 LISTING_BUSY. Only then does the worker start (so the busy
# check never races the stub), the job succeeds and the listing is `validated`. A reviewer may not
# generate (403) and bharat gets 404 on acme's listing. One read-only `db_*` step checks the rows
# (one version, a rules_engine run, the agent runs). The failed leg reuses the operator session:
# `stub:failed` ends the job `failed` with GENERATION_FAILED and the listing
# `failed/generation_failed`, with no version and the agent runs kept (version NULL).
#
# Usage: WORKER_START=<cmd> WORKER_STOP=<cmd> scripts/e2e/generate.sh [base_url]
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

step generate_202
key="e2e-gen-$run"
request="{\"product_id\": \"$product\", \"marketplace\": \"amazon\"}"
call POST /listings/generate "$operator" "$key" "$request"
expect 202
job_id="$(json 'b["job_id"]')"
listing_id="$(json 'b["listing_id"]')"
json 'b["poll_url"] == "/api/v1/jobs/" + b["job_id"]' >/dev/null

step replay_same_202
call POST /listings/generate "$operator" "$key" "$request"
expect 202
json 'b["job_id"] == "'"$job_id"'" and b["listing_id"] == "'"$listing_id"'"' >/dev/null

step new_key_busy_409
call POST /listings/generate "$operator" "e2e-gen-busy-$run" "$request"
expect 409
json 'b["error"]["type"] == "LISTING_BUSY"' >/dev/null

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

step poll_job_succeeded
wait_job "$operator" "$job_id" succeeded

step status_validated
call GET "/listings/$listing_id/status" "$operator"
expect 200
json 'b["status"] == "validated" and b["listing_id"] == "'"$listing_id"'"' >/dev/null

step login_acme_reviewer
reviewer="$(login reviewer@acme.test)"

step reviewer_generate_403
call POST /listings/generate "$reviewer" "e2e-gen-reviewer-$run" "$request"
expect 403

step login_bharat_operator
bharat="$(login operator@bharat.test)"

step bharat_status_404
call GET "/listings/$listing_id/status" "$bharat"
expect 404

step db_version_and_runs
[ "$(sql "SELECT count(*) FROM listing_versions WHERE listing_id = '$listing_id'")" = "1" ] \
  || fail "want one listing version"
[ "$(sql "SELECT count(*) >= 1 FROM validation_runs WHERE listing_id = '$listing_id' AND source = 'rules_engine'")" = "t" ] \
  || fail "want a rules_engine validation run"
[ "$(sql "SELECT count(*) >= 1 FROM agent_runs WHERE job_id = '$job_id'")" = "t" ] \
  || fail "want the job's agent runs"

step create_product_failed_201
failed_product="$(new_product "E2E-GEN-$(date +%s)-$RANDOM")"

step generate_stub_failed_202
call POST /listings/generate "$operator" "e2e-gen-failed-$run" \
  "{\"product_id\": \"$failed_product\", \"marketplace\": \"amazon\", \"user_instructions\": \"stub:failed\"}"
expect 202
failed_job="$(json 'b["job_id"]')"
failed_listing="$(json 'b["listing_id"]')"

step poll_job_failed
wait_job "$operator" "$failed_job" failed
json 'b["error"]["error_code"] == "GENERATION_FAILED" and b["error"]["status"] == "failed"' >/dev/null

step status_failed_generation_failed
call GET "/listings/$failed_listing/status" "$operator"
expect 200
json 'b["status"] == "failed" and b["status_reason"] == "generation_failed"' >/dev/null

step job_result_no_version
call GET "/jobs/$failed_job" "$operator"
expect 200
json '"listing_version_id" not in str(b) and b["result_url"] is None' >/dev/null

step db_failed_runs
[ "$(sql "SELECT count(*) FROM listing_versions WHERE listing_id = '$failed_listing'")" = "0" ] \
  || fail "want no listing version"
[ "$(sql "SELECT count(*) >= 1 AND bool_and(listing_version_id IS NULL) FROM agent_runs WHERE job_id = '$failed_job'")" = "t" ] \
  || fail "want the failed job's agent runs kept with no version"

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

echo "e2e: generate OK (listing $listing_id)"
