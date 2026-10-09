#!/usr/bin/env bash
# S23 analytics, HTTP only (CODE-PLAN S23 T12; AC 14): acme snapshots its analytics, then an
# operator generates three listings on the stub pipeline, a reviewer approves two and rejects one,
# and the next snapshot moves by exactly that (generated +3, approvals +2, rejections +1,
# first-pass validation runs +3, publish counts unchanged). bharat's analytics never move. No
# token is 401, `from` after `to` is 422, both routes answer the same body, and the staff cost page
# shows one generated listing's cost and refuses a bad day.
#
# Publish legs: the publish path (POST /listings/publish, its submissions and the fake marketplace
# forced failure) is S21's and is not on this base, so this script asserts the publish counts stay
# unchanged; S21's hand-off adds the publish_2 / forced-failure legs and their +1/+1 deltas.
#
# Usage: WORKER_START=<cmd> WORKER_STOP=<cmd> scripts/e2e/analytics.sh [base_url]
# No other worker may run against the stack (`make dev` starts none). Local only: the base URL must
# be on 127.0.0.1 or localhost, else exit 2 before any request. One login per email per run (the
# `make e2e` loop resets the limiter before each script); every poll gives up after 60 s. The
# range is yesterday..tomorrow (UTC), so a run across midnight still counts its own rows. On
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
admin="$base/internal/admin"
password="devpassword123" # the demo seed's local-only password (A§5)
run="$(date +%s)-$RANDOM"
category="ethnic_wear_kurta_sets" # the seeded category with a current Amazon template
POLL_LIMIT_S=60
range="$(python3 -c 'import datetime as d; t = d.datetime.now(d.timezone.utc).date(); print(f"from={t - d.timedelta(days=1)}&to={t + d.timedelta(days=1)}")')"

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

login() {
  call POST /auth/login "" "" "{\"email\": \"$1\", \"password\": \"$password\"}"
  expect 200
  json 'b["access_token"]'
}

# snapshot TOKEN FILE: GET /analytics for the run's range into FILE.
snapshot() {
  call GET "/analytics?$range" "$1"
  expect 200
  json 'set(b["counts"]) == {"listings_generated", "approvals", "rejections", "validation_first_pass_passed", "validation_first_pass_total", "publish_succeeded", "publish_failed"}' >/dev/null
  cp "$body" "$2"
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

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step login_acme_operator
operator="$(login operator@acme.test)"

step login_acme_reviewer
reviewer="$(login reviewer@acme.test)"

step login_bharat_operator
bharat="$(login operator@bharat.test)"

step snapshot_acme
snapshot "$operator" "$work/acme_before.json"

step snapshot_bharat
snapshot "$bharat" "$work/bharat_before.json"

step no_token_401
call GET "/analytics?$range"
expect 401
json 'b["error"]["type"] == "UNAUTHENTICATED"' >/dev/null

step from_after_to_422
call GET "/analytics?from=2026-10-02&to=2026-10-01" "$operator"
expect 422
json 'b["error"]["type"] == "VALIDATION_FAILED"' >/dev/null

step alias_equals_summary
call GET "/analytics/summary?$range" "$operator"
expect 200
cmp -s "$body" "$work/acme_before.json" || fail "/analytics/summary answered another body"

step create_3_products
products=()
for n in 1 2 3; do
  call POST /products "$operator" "" "{\"sku\": \"E2E-AN-$run-$n\", \"title\": \"E2E Analytics Kurta Set $n\", \"category\": \"$category\"}"
  expect 201
  products+=("$(json 'b["id"]')")
done

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

step generate_3
listings=()
versions=()
for n in 0 1 2; do
  call POST /listings/generate "$operator" "e2e-an-gen-$run-$n" "{\"product_id\": \"${products[$n]}\", \"marketplace\": \"amazon\"}"
  expect 202
  job="$(json 'b["job_id"]')"
  listings+=("$(json 'b["listing_id"]')")
  wait_job "$operator" "$job" succeeded
  call GET "/listings/${listings[$n]}" "$operator"
  expect 200
  json 'b["status"] == "validated"' >/dev/null
  versions+=("$(json 'b["current_version_id"]')")
done

step reviewer_approve_2
for n in 0 1; do
  call POST /listings/approve "$reviewer" "e2e-an-ok-$run-$n" "{\"items\": [{\"listing_id\": \"${listings[$n]}\", \"listing_version_id\": \"${versions[$n]}\", \"decision\": \"approve\"}]}"
  expect 200
  json 'b["items"][0]["http_status"] == 200' >/dev/null
done

step reviewer_reject_1
call POST /listings/approve "$reviewer" "e2e-an-rej-$run" "{\"items\": [{\"listing_id\": \"${listings[2]}\", \"listing_version_id\": \"${versions[2]}\", \"decision\": \"reject\", \"reason_code\": \"poor_quality\", \"feedback\": \"Shorter title, please.\"}]}"
expect 200
json 'b["items"][0]["http_status"] == 200' >/dev/null

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

step snapshot_acme_after
snapshot "$operator" "$work/acme_after.json"

step assert_deltas
python3 - "$work/acme_before.json" "$work/acme_after.json" <<'PY' || fail "the analytics deltas are wrong"
import json, sys

before, after = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
want = {
    "listings_generated": 3,
    "approvals": 2,
    "rejections": 1,
    "validation_first_pass_total": 3,
    "publish_succeeded": 0,
    "publish_failed": 0,
}
delta = {k: after["counts"][k] - before["counts"][k] for k in want}
assert delta == want, delta
assert after["listings_generated"] == after["counts"]["listings_generated"]
amazon = lambda body: next(  # noqa: E731
    (r["counts"] for r in body["by_marketplace"] if r["marketplace"] == "amazon"),
    dict.fromkeys(want, 0),
)
assert {k: amazon(after)[k] - amazon(before)[k] for k in want} == want
a, r = after["counts"]["approvals"], after["counts"]["rejections"]
assert after["approval_rate"] == round(a / (a + r), 4), after["approval_rate"]
PY

step bharat_unchanged
snapshot "$bharat" "$work/bharat_after.json"
cmp -s "$work/bharat_before.json" "$work/bharat_after.json" || fail "bharat's analytics moved"

step staff_cost_listing_200
status="$(curl -s -o "$body" -w '%{http_code}' "$admin/ops/cost/listings/${listings[0]}")" || status="000"
expect 200
grep -qF 'data-ops="listing-cost"' "$body" || fail "the listing cost page lacks its block"

step staff_cost_bad_day_400
status="$(curl -s -o /dev/null -w '%{http_code}' "$admin/ops/cost?day=2026-13-01")" || status="000"
expect 400

echo "e2e: analytics OK (listings ${listings[*]})"
