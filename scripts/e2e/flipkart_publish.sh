#!/usr/bin/env bash
# The S22 Flipkart publish e2e, HTTP only, against the local fake marketplace (S22 T23; AC20):
# an acme owner creates a kurta product and, on a second API with FLIPKART_ENABLED=false,
# generates its Flipkart listing (generation is not gated); that API then answers the publish
# with HTTP 202 and an item 501 "Flipkart is not enabled" (no job, the listing unchanged, no
# submission) and retry-fields with the whole-request 501, and the fake sees no Flipkart call.
# On the flag-on API (`make dev` turns the flag on locally) the owner connects Flipkart, the
# reviewer approves the same listing, the fake refuses two of its attributes with a retryable
# code (the fake error map), the publish is partial, retry-fields resends exactly those two
# attributes, a refresh-status syncs the listing to live, a replay of the publish key answers the
# same body, and the account is revoked.
#
# Usage: FAKE_URL=<fake> FLAG_OFF_URL=<flag-off API> WORKER_START=<cmd> WORKER_STOP=<cmd> \
#        ERRORS_LOAD=<cmd> scripts/e2e/flipkart_publish.sh [base_url]
# `make e2e` sets every hook: the fake for the whole pass, the flag-off API on
# FLAG_OFF_API_HOST_PORT, and a worker pointed at the fake. Local only: any other host exits 2
# before a request; a missing hook exits 2. Every wait is bounded at 60 s. On failure it prints
# the step name and the HTTP status seen, never a response body. Needs the demo seed.
# Post-condition: no live acme Flipkart account is left (the last step revokes it).
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  echo "usage: FAKE_URL=<fake> FLAG_OFF_URL=<flag-off API> WORKER_START=<cmd> WORKER_STOP=<cmd> ERRORS_LOAD=<cmd> $0 [base_url] (make e2e sets them)" >&2
  exit 2
}
[ -n "${FAKE_URL:-}" ] && [ -n "${FLAG_OFF_URL:-}" ] && [ -n "${WORKER_START:-}" ] \
  && [ -n "${WORKER_STOP:-}" ] && [ -n "${ERRORS_LOAD:-}" ] || usage

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
fake="$FAKE_URL"
python3 scripts/ci/local_url.py "$fake" || exit 2
fake="${fake%/}"
off="$FLAG_OFF_URL"
python3 scripts/ci/local_url.py "$off" || exit 2
off="${off%/}"
api="$base/api/v1"
off_api="$off/api/v1"
password="devpassword123" # the demo seed's local-only password (A§5)
run="$(date +%s)$RANDOM"
sku="E2E-FK-$run"
seller="E2EFKPUB$run"
category="ethnic_wear_kurta_sets" # the seeded category with a current Flipkart kurta template
error_map="apps/api/app/db/seed_data/error_map.fake.json"
bound=60

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
location=""

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

# call METHOD URL [TOKEN] [KEY] [JSON]: status in $status, body in $body, Location in $location.
# Redirects are never followed.
call() {
  local method="$1" url="$2" token="${3:-}" key="${4:-}" data="${5:-}" out
  local args=(-s -o "$body" -w '%{http_code} %{redirect_url}' --max-time "$bound" -X "$method"
    -H 'Accept: application/json')
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$key" ]; then args+=(-H "Idempotency-Key: $key"); fi
  if [ -n "$data" ]; then args+=(-H 'Content-Type: application/json' --data-binary "$data"); fi
  out="$(curl "${args[@]}" "$url")" || out="000 "
  status="${out%% *}"
  location="${out#* }"
}

# json EXPR: evaluate a Python expression over the parsed body `b`; prints the result.
json() {
  python3 - "$body" "$1" <<'PY' || fail "unexpected response shape"
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        b = json.load(fh)
    scope = {"__builtins__": {}, "b": b, "len": len, "str": str, "all": all, "any": any,
             "set": set, "sorted": sorted, "loads": json.loads}
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

# query URL NAME: one query parameter of URL (empty when absent).
query() {
  python3 - "$1" "$2" <<'PY'
import sys
from urllib.parse import parse_qs, urlsplit
print(parse_qs(urlsplit(sys.argv[1]).query).get(sys.argv[2], [""])[0])
PY
}

# path_query URL: the path and query of URL (the callback goes to this run's own base URL).
path_query() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import urlsplit
parts = urlsplit(sys.argv[1])
print(parts.path + ("?" + parts.query if parts.query else ""))
PY
}

login() {
  call POST "$api/auth/login" "" "" "{\"email\": \"$1\", \"password\": \"$password\"}"
  expect 200
  json 'b["access_token"]'
}

# wait_job API TOKEN JOB STATUS: poll the job envelope until STATUS (at most $bound s).
wait_job() {
  local deadline=$(( $(date +%s) + bound )) seen
  while :; do
    call GET "$1/jobs/$3" "$2"
    expect 200
    seen="$(json 'b["status"]')"
    if [ "$seen" = "$4" ]; then return 0; fi
    case "$seen" in succeeded|partial|failed|cancelled|dead_letter) fail "the job ended $seen, want $4" ;; esac
    if [ "$(date +%s)" -ge "$deadline" ]; then fail "the job did not reach $4 within ${bound} s"; fi
    sleep 1
  done
}

# wait_listing TOKEN LISTING EXPR: poll the listing until EXPR holds (at most $bound s).
wait_listing() {
  local deadline=$(( $(date +%s) + bound ))
  while :; do
    call GET "$api/listings/$2" "$1"
    expect 200
    if json "$3" >/dev/null 2>&1; then return 0; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then fail "the listing did not reach the state within ${bound} s"; fi
    sleep 1
  done
}

fake_control() {
  call POST "$fake/__control" "" "" "$1"
  expect 200
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$bound" "$base/readyz")" || status="000"
expect 200

step fake_health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$bound" "$fake/healthz")" || status="000"
expect 200

step off_health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$bound" "$off/readyz")" || status="000"
expect 200

step errors_load
bash -c "$ERRORS_LOAD" errors_load "$error_map" >/dev/null || fail "the ERRORS_LOAD hook failed"

step login_acme_owner
owner="$(login owner@acme.test)"

step fake_reset_calls
fake_control '{"reset_calls": true}'

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

step create_product
call POST "$api/products" "$owner" "" "{\"sku\": \"$sku\", \"title\": \"E2E Flipkart Kurta Set\", \"category\": \"$category\", \"attributes\": {\"color\": \"blue\", \"fabric_type\": \"cotton\"}}"
expect 201
product="$(json 'b["id"]')"

step off_generate_flipkart_202
call POST "$off_api/listings/generate" "$owner" "e2e-fk-gen-$run" "{\"product_id\": \"$product\", \"marketplace\": \"flipkart\"}"
expect 202
gen_job="$(json 'b["job_id"]')"
listing="$(json 'b["listing_id"]')"

step off_wait_generated
wait_job "$off_api" "$owner" "$gen_job" succeeded
call GET "$off_api/listings/$listing" "$owner"
expect 200
json 'b["marketplace"] == "flipkart" and b["status"] in ("validated", "needs_review")' >/dev/null \
  || fail "the generation did not validate"
generated="$(json 'b["status"]')"
version="$(json 'b["current_version_id"]')"

step off_publish_item_501
call POST "$off_api/listings/publish" "$owner" "e2e-fk-off-pub-$run" "{\"items\": [{\"listing_id\": \"$listing\"}]}"
expect 202
json 'b["items"][0]["http_status"] == 501 and b["items"][0]["job_id"] is None' >/dev/null
json 'b["items"][0]["error"]["error_code"] == "NOT_IMPLEMENTED"' >/dev/null
json 'b["items"][0]["error"]["errors"][0]["message"] == "Flipkart is not enabled"' >/dev/null

step off_status_unchanged
call GET "$off_api/listings/$listing" "$owner"
expect 200
json 'b["status"] == "'"$generated"'"' >/dev/null

step off_submissions_empty
call GET "$off_api/listings/$listing/submissions" "$owner"
expect 200
json 'b["data"] == []' >/dev/null

step off_retry_fields_501
call POST "$off_api/listings/$listing/publish/retry-fields" "$owner" "e2e-fk-off-retry-$run"
expect 501
json 'b["error"]["type"] == "NOT_IMPLEMENTED" and b["error"]["message"] == "Flipkart is not enabled"' >/dev/null

step fake_calls_0
call GET "$fake/__calls?marketplace=flipkart"
expect 200
json 'b["count"] == 0' >/dev/null

step cleanup_live_flipkart
call GET "$api/marketplaces/accounts?limit=200" "$owner"
expect 200
for stale in $(json '" ".join(a["id"] for a in b["data"] if a["marketplace"] == "flipkart" and a["status"] != "revoked")'); do
  call POST "$api/marketplaces/accounts/$stale/revoke" "$owner"
  expect 200
done

step connect
call POST "$api/marketplaces/connect" "$owner" "" '{"marketplace": "flipkart"}'
expect 200
authorize_url="$(json 'b["authorize_url"]')"
case "$authorize_url" in "$fake"/*) ;; *) fail "the consent URL is not the fake's" ;; esac

step consent
call GET "$authorize_url&seller=$seller"
expect 302
callback="$base$(path_query "$location")"

step callback
call GET "$callback"
expect 303
handle="$(query "$location" pending)"
[ -n "$handle" ] || fail "no pending handle"

step confirm
call POST "$api/marketplaces/connect/confirm" "$owner" "" "{\"pending\": \"$handle\"}"
expect 200
account="$(json 'b["id"]')"
json 'b["marketplace"] == "flipkart" and b["status"] == "active"' >/dev/null

step login_acme_reviewer
reviewer="$(login reviewer@acme.test)"

step approve_200
call POST "$api/listings/approve" "$reviewer" "e2e-fk-approve-$run" \
  "{\"items\": [{\"listing_id\": \"$listing\", \"listing_version_id\": \"$version\", \"decision\": \"approve\"}]}"
expect 200
json 'b["items"][0]["http_status"] == 200' >/dev/null || fail "the reviewer's approve was refused"

step fake_fail_2_fields
fake_control "{\"fk_errors\": {\"$sku\": {\"title\": \"FAKE-FK-QUOTA\", \"description\": \"FAKE-FK-QUOTA\"}}}"

step publish_202
items="{\"items\": [{\"listing_id\": \"$listing\", \"listing_version_id\": \"$version\"}]}"
call POST "$api/listings/publish" "$owner" "e2e-fk-pub-$run" "$items"
expect 202
json 'b["items"][0]["http_status"] == 202 and b["items"][0]["listing_status"] == "publishing"' >/dev/null
job="$(json 'b["items"][0]["job_id"]')"
cp "$body" "$work/publish.json"

step wait_partial
wait_job "$api" "$owner" "$job" partial
wait_listing "$owner" "$listing" 'b["status"] == "failed" and b["status_reason"] == "publish_partial"'
call GET "$api/listings/$listing/submissions" "$owner"
expect 200
json 'b["data"][0]["outcome"] == "partially_accepted" and sorted(f["field"] for f in b["data"][0]["items"][0]["fields"] if f["status"] == "failed") == ["content.description", "content.title"]' >/dev/null

step retry_fields_202
call POST "$api/listings/$listing/publish/retry-fields" "$owner" "e2e-fk-retry-$run"
expect 202
job="$(json 'b["job_id"]')"
wait_job "$api" "$owner" "$job" succeeded
wait_listing "$owner" "$listing" 'b["status"] == "published"'

step fake_body_two_attrs
call GET "$fake/__calls?marketplace=flipkart"
expect 200
json 'b["calls"][-1]["path"].endswith("/listings/v3/update")' >/dev/null || fail "the retry was not an update"
json '[(e["sku"], sorted(e["attributes"])) for e in loads(b["calls"][-1]["body"])["listings"]] == [("'"$sku"'", ["description", "title"])]' >/dev/null \
  || fail "the retry sent more than the two failed attributes"

step sync_once
fake_control "{\"fk_state\": {\"$sku\": \"ACTIVE\"}}"
call POST "$api/listings/$listing/refresh-status" "$owner" "e2e-fk-refresh-$run"
expect 202
sync_job="$(json 'b["job_id"]')"
wait_job "$api" "$owner" "$sync_job" succeeded

step state_live
wait_listing "$owner" "$listing" 'b["status"] == "published" and b["marketplace_state"] == "live"'

step publish_replay_same
call POST "$api/listings/publish" "$owner" "e2e-fk-pub-$run" "$items"
expect 202
cmp -s "$body" "$work/publish.json" || fail "the replay answered another body"

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

step revoke_200
call POST "$api/marketplaces/accounts/$account/revoke" "$owner"
expect 200
json 'b["status"] == "revoked"' >/dev/null

echo "e2e: flipkart_publish OK ($listing)"
