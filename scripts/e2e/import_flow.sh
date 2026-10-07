#!/usr/bin/env bash
# The S13b CSV import, HTTP only (CODE-PLAN S13 B15; AC 22): an acme operator uploads the 50-row
# fixture (5 bad rows), the job ends `partial`, the 5 failed rows read back, a replay of the key
# returns the same ids and the key with another file is 409, bharat gets 404 on the import; after
# `templates load` a rows retry fixes the unmapped category (46 imported, 4 failed, `partial`);
# with the worker stopped a second upload stays queued and is cancelled (200 `cancelled`).
#
# Usage: WORKER_START=<cmd> WORKER_STOP=<cmd> TEMPLATES_LOAD=<cmd> scripts/e2e/import_flow.sh [base_url]
# Each hook is a shell command; TEMPLATES_LOAD gets the templates file as "$1". The script itself
# only speaks HTTP: the hooks start and stop the worker and run the migrator's `templates load`.
# No worker but the hook's may run against the stack: the cancel leg checks its job is still
# `queued` and exits 1 otherwise. Every poll gives up after 120 s. Local only (the demo seed's
# public password): any other host exits 2 before a request. Rerunnable: every run uses fresh
# SKUs, a fresh category and fresh Idempotency-Keys.
# On failure it prints the step name and the HTTP status seen, never a response body.
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  echo "usage: WORKER_START=<cmd> WORKER_STOP=<cmd> TEMPLATES_LOAD=<cmd> $0 [base_url]" >&2
  exit 2
}
[ -n "${WORKER_START:-}" ] && [ -n "${WORKER_STOP:-}" ] && [ -n "${TEMPLATES_LOAD:-}" ] || usage

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
api="$base/api/v1"
password="devpassword123" # the demo seed's local-only password (A§5)
run="$(date +%s)$RANDOM"
POLL_LIMIT_S=120

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

# call METHOD PATH [TOKEN] [KEY]: the status goes in $status, the body in $body.
call() {
  local method="$1" path="$2" token="${3:-}" key="${4:-}"
  local args=(-s -o "$body" -w '%{http_code}' -X "$method" -H 'Accept: application/json')
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$key" ]; then args+=(-H "Idempotency-Key: $key"); fi
  status="$(curl "${args[@]}" "$api$path")" || status="000"
}

# upload TOKEN KEY FILE: the multipart CSV upload.
upload() {
  status="$(curl -s -o "$body" -w '%{http_code}' -X POST -H 'Accept: application/json' \
    -H "Authorization: Bearer $1" -H "Idempotency-Key: $2" \
    -F "file=@$3;type=text/csv" "$api/products/bulk")" || status="000"
}

login() {
  status="$(curl -s -o "$body" -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    --data-binary "{\"email\": \"$1\", \"password\": \"$password\"}" "$api/auth/login")" || status="000"
  expect 200
  json 'b["access_token"]'
}

expect() {
  if [ "$status" != "$1" ]; then fail "want HTTP $1"; fi
}

# json EXPR: evaluate a Python expression over the parsed body `b`; prints the result.
json() {
  python3 - "$body" "$1" <<'PY' || fail "unexpected response shape"
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        b = json.load(fh)
    scope = {"__builtins__": {}, "b": b, "len": len, "str": str, "all": all}
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

# The fixture, with this run's SKUs and a category no template maps yet.
category="e2e_cat_$run"
python3 - apps/api/tests/fixtures/imports/products_50_5_bad.csv "$work/products.csv" "$run" "$category" <<'PY'
import csv, sys
src, dst, run, category = sys.argv[1:5]
with open(src, encoding="utf-8", newline="") as fh:
    rows = list(csv.DictReader(fh))
for row in rows:
    row["seller_sku"] = f"E2E{run}-{row['seller_sku']}"
    if row["category"] == "ethnic_wear_dupattas":
        row["category"] = category
with open(dst, "w", encoding="utf-8", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
PY
printf 'seller_sku,product_name,category,main_image\nE2E%s-OTHER,Other,ethnic_wear_kurta_sets,https://cdn.example.com/o.jpg\n' "$run" >"$work/other.csv"
printf 'seller_sku,product_name,category,main_image\nE2E%s-LATE,Late,ethnic_wear_kurta_sets,https://cdn.example.com/l.jpg\n' "$run" >"$work/late.csv"
printf '[{"version": 1, "product_type": "E2E", "template": {"marketplace": "amazon", "category": "%s", "required_attributes": [], "optional_attributes": [], "specs": {}}}]\n' "$category" >"$work/templates.json"

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step login_acme_operator
operator="$(login operator@acme.test)"

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

step upload_202
key="e2e-import-$run"
upload "$operator" "$key" "$work/products.csv"
expect 202
import_id="$(json 'b["import_id"]')"
job_id="$(json 'b["job_id"]')"

step job_partial
wait_job "$operator" "$job_id" partial

step report_45_5
call GET "/imports/$import_id" "$operator"
expect 200
json 'b["total_rows"] == 50 and b["imported"] == 45 and b["failed"] == 5 and b["rows"] == []' >/dev/null

step failed_rows_5
call GET "/imports/$import_id/rows?status=failed" "$operator"
expect 200
json '[r["row_number"] for r in b["data"]] == [8, 15, 22, 35, 43] and all(r["errors"] for r in b["data"])' >/dev/null

step replay_same_ids
upload "$operator" "$key" "$work/products.csv"
expect 202
json 'b["import_id"] == "'"$import_id"'" and b["job_id"] == "'"$job_id"'"' >/dev/null

step key_reuse_409
upload "$operator" "$key" "$work/other.csv"
expect 409
json 'b["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"' >/dev/null

step bharat_import_404
bharat="$(login operator@bharat.test)"
call GET "/imports/$import_id" "$bharat"
expect 404

step templates_load
bash -c "$TEMPLATES_LOAD" templates_load "$work/templates.json" || fail "the TEMPLATES_LOAD hook failed"

step retry_202
call POST "/imports/$import_id/rows/retry" "$operator" "e2e-retry-$run"
expect 202
retry_job="$(json 'b["job_id"]')"

step retry_partial_46_4
wait_job "$operator" "$retry_job" partial
call GET "/imports/$import_id" "$operator"
expect 200
json 'b["total_rows"] == 50 and b["imported"] == 46 and b["failed"] == 4' >/dev/null

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

step upload_queued
upload "$operator" "e2e-late-$run" "$work/late.csv"
expect 202
late_job="$(json 'b["job_id"]')"
call GET "/jobs/$late_job" "$operator"
expect 200
if [ "$(json 'b["status"]')" != "queued" ]; then
  fail "a worker outside WORKER_START is running"
fi

step cancel_200
call POST "/jobs/$late_job/cancel" "$operator" "e2e-cancel-$run"
expect 200
json 'b["status"] == "cancelled"' >/dev/null

echo "e2e: import_flow OK (import $import_id)"
