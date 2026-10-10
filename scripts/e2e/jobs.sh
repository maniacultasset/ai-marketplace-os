#!/usr/bin/env bash
# Jobs, HTTP only (S26 T22; AC 10): with no worker running, an acme operator uploads a one-row CSV
# (202) and polls its job (queued, twice); bharat gets 404 on the job and on its cancel; the
# operator cancels it (200 `cancelled`; a replay of the key answers the same), a second cancel
# under a new key is 409, and the job stays `cancelled`.
#
# Usage: scripts/e2e/jobs.sh [base_url]
# No worker may run against the stack (`make e2e` starts none; the other scripts' hooks stop
# theirs): the cancel leg checks the job is still `queued` and exits 1 otherwise. Rerunnable: a
# run-unique SKU and Idempotency-Key. 2 logins (the demo seed's public password); polls 1 s apart.
# Local only: any other host exits 2 before a request. On failure it prints the step name and the
# HTTP status seen, never a body.
set -euo pipefail
cd "$(dirname "$0")/../.."

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
api="$base/api/v1"
password="devpassword123" # the demo seed's local-only password (A§5)
run="$(date +%s)$RANDOM"

work="$(mktemp -d)"
chmod 0700 "$work"
trap 'rm -rf "$work"' EXIT
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

# call METHOD PATH [TOKEN] [KEY] [JSON_FILE]: the status goes in $status, the body in $body. A
# JSON body always comes from a file, so no password or token is ever on an argv.
call() {
  local method="$1" path="$2" token="${3:-}" key="${4:-}" data="${5:-}"
  local args=(-s -o "$body" -w '%{http_code}' --max-time 60 -X "$method" -H 'Accept: application/json')
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$key" ]; then args+=(-H "Idempotency-Key: $key"); fi
  if [ -n "$data" ]; then args+=(-H 'Content-Type: application/json' --data-binary "@$data"); fi
  status="$(curl "${args[@]}" "$api$path")" || status="000"
}

# json EXPR: evaluate a Python expression over the parsed body `b`; prints the result; a false
# or failing expression fails the step.
json() {
  python3 - "$body" "$1" <<'PY' || fail "unexpected response shape"
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        b = json.load(fh)
    scope = {"__builtins__": {}, "b": b, "len": len, "str": str, "all": all, "any": any,
             "set": set, "sorted": sorted}
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


login() {
  printf '{"email": "%s", "password": "%s"}' "$1" "$password" >"$work/login.json"
  call POST /auth/login "" "" "$work/login.json"
  expect 200
  json 'b["access_token"]'
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "$base/readyz")" || status="000"
expect 200

step login_acme_operator
operator="$(login operator@acme.test)"

step login_bharat_operator
bharat="$(login operator@bharat.test)"

step upload_202
printf 'seller_sku,product_name,category,mrp,selling_price,main_image\nE2E-JOBS-%s,Jobs Kurta Set,ethnic_wear_kurta_sets,1299,999,https://cdn.example.com/jobs/%s.jpg\n' \
  "$run" "$run" >"$work/one.csv"
status="$(curl -s -o "$body" -w '%{http_code}' --max-time 60 -X POST -H 'Accept: application/json' \
  -H "Authorization: Bearer $operator" -H "Idempotency-Key: e2e-jobs-$run" \
  -F "file=@$work/one.csv;type=text/csv" "$api/products/bulk")" || status="000"
expect 202
job_id="$(json 'b["job_id"]')"
json 'b["poll_url"] == "/api/v1/jobs/" + b["job_id"]' >/dev/null

step poll_queued
for _ in 1 2; do
  call GET "/jobs/$job_id" "$operator"
  expect 200
  json 'b["status"] == "queued" and b["job_id"] == "'"$job_id"'" and b["kind"] == "bulk_import"' >/dev/null \
    || fail "the job is not queued (is a worker running?)"
  sleep 1
done

step bharat_get_404
call GET "/jobs/$job_id" "$bharat"
expect 404
json 'b["error"]["type"] == "NOT_FOUND"' >/dev/null

step bharat_cancel_404
call POST "/jobs/$job_id/cancel" "$bharat" "e2e-jobs-cancel-bharat-$run"
expect 404

step cancel_200
call POST "/jobs/$job_id/cancel" "$operator" "e2e-jobs-cancel-$run"
expect 200
json 'b["status"] == "cancelled"' >/dev/null

step cancel_replay_same_body
call POST "/jobs/$job_id/cancel" "$operator" "e2e-jobs-cancel-$run"
expect 200
json 'b["status"] == "cancelled"' >/dev/null

step cancel_again_409
call POST "/jobs/$job_id/cancel" "$operator" "e2e-jobs-cancel-again-$run"
expect 409

step still_cancelled
call GET "/jobs/$job_id" "$operator"
expect 200
json 'b["status"] == "cancelled" and b["finished_at"] is not None' >/dev/null

echo "e2e: jobs OK"
