#!/usr/bin/env bash
# The S21 publish flow, HTTP only against the local fake marketplace (CODE-PLAN S21 T25; AC15):
# an acme owner connects Amazon (its own seller, so the script stands alone); an operator generates
# a listing (stub pipeline) and a second member, the reviewer, approves it; the reviewer may not
# publish (403); the operator publishes (202, one job), a replay of the key answers the same body
# and another body under it is 409; the job submits through the fake SP-API and the listing is
# `published` + `processing` ("Submitted", not live); the fake reports the SKU buyable, a
# refresh-status (202) syncs it to `live` and a second refresh within 5 minutes is 429. A second
# listing then has two of its fields throttled by the fake (a retryable code from the fake error
# map): the publish is partial (`failed/publish_partial`), the submissions show the two failed
# fields, retry-fields (202) resends exactly those two paths as one PATCH and the listing is
# `published`. No token is 401; bharat gets 404 for acme's listing and its submissions. The run's
# SKUs are removed from the fake at the end and acme's Amazon account is revoked.
#
# Usage: FAKE_URL=<fake> WORKER_START=<cmd> WORKER_STOP=<cmd> ERRORS_LOAD=<cmd> \
#        scripts/e2e/publish_flow.sh [base_url]
# `make e2e` sets every hook (and points the API and the worker at the fake: OAuth and SP-API).
# Local only: any other host exits 2 before a request; a missing hook exits 2. Every wait is
# bounded at 60 s. On failure it prints the step name and the HTTP status, never a body.
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  echo "usage: FAKE_URL=<fake> WORKER_START=<cmd> WORKER_STOP=<cmd> ERRORS_LOAD=<cmd> $0 [base_url]" >&2
  exit 2
}
[ -n "${FAKE_URL:-}" ] && [ -n "${WORKER_START:-}" ] && [ -n "${WORKER_STOP:-}" ] \
  && [ -n "${ERRORS_LOAD:-}" ] || usage

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
fake="${FAKE_URL%/}"
python3 scripts/ci/local_url.py "$fake" || exit 2
api="$base/api/v1"
password="devpassword123" # the demo seed's local-only password (A§5)
run="$(date +%s)$RANDOM"
prefix="E2E-PUB-$run"
seller="E2EPUB$run"
category="ethnic_wear_kurta_sets" # the seeded category with a current, mapped Amazon template
error_map="apps/api/app/db/seed_data/error_map.fake.json"
bound=60

work="$(mktemp -d)"
worker_running=0
cleanup() {
  curl -s -o /dev/null --max-time 10 -X POST -H 'Content-Type: application/json' \
    --data-binary "{\"prefix\": \"$prefix\"}" "$fake/__control/reset" || true
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

query() {
  python3 - "$1" "$2" <<'PY'
import sys
from urllib.parse import parse_qs, urlsplit
print(parse_qs(urlsplit(sys.argv[1]).query).get(sys.argv[2], [""])[0])
PY
}

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

# connect_confirm TOKEN: connect -> consent (as $seller) -> callback -> confirm; the account id.
connect_confirm() {
  local token="$1" url callback handle
  call POST "$api/marketplaces/connect" "$token" "" '{"marketplace": "amazon"}'
  expect 200
  url="$(json 'b["authorize_url"]')"
  case "$url" in "$fake"/*) ;; *) fail "the consent URL is not the fake's" ;; esac
  call GET "$url&seller=$seller"
  expect 302
  callback="$base$(path_query "$location")"
  call GET "$callback"
  expect 303
  handle="$(query "$location" pending)"
  [ -n "$handle" ] || fail "no pending handle"
  call POST "$api/marketplaces/connect/confirm" "$token" "" "{\"pending\": \"$handle\"}"
  expect 200
  json 'b["id"]'
}

# wait_job TOKEN JOB STATUS: poll the job envelope until STATUS (at most $bound s).
wait_job() {
  local deadline=$(( $(date +%s) + bound )) seen
  while :; do
    call GET "$api/jobs/$2" "$1"
    expect 200
    seen="$(json 'b["status"]')"
    if [ "$seen" = "$3" ]; then return 0; fi
    case "$seen" in succeeded|partial|failed|cancelled|dead_letter) fail "the job ended $seen, want $3" ;; esac
    if [ "$(date +%s)" -ge "$deadline" ]; then fail "the job did not reach $3 within ${bound} s"; fi
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

# generated_approved SKU: a product, a stub generation and the reviewer's approval; sets
# $listing_id and $version.
generated_approved() {
  call POST "$api/products" "$operator" "" "{\"sku\": \"$1\", \"title\": \"E2E Publish Kurta Set\", \"category\": \"$category\"}"
  expect 201
  local product job
  product="$(json 'b["id"]')"
  call POST "$api/listings/generate" "$operator" "e2e-pub-gen-$1" "{\"product_id\": \"$product\", \"marketplace\": \"amazon\"}"
  expect 202
  job="$(json 'b["job_id"]')"
  listing_id="$(json 'b["listing_id"]')"
  wait_job "$operator" "$job" succeeded
  call GET "$api/listings/$listing_id" "$operator"
  expect 200
  json 'b["status"] in ("validated", "needs_review")' >/dev/null || fail "the generation did not validate"
  version="$(json 'b["current_version_id"]')"
  call POST "$api/listings/approve" "$reviewer" "e2e-pub-approve-$1" \
    "{\"items\": [{\"listing_id\": \"$listing_id\", \"listing_version_id\": \"$version\", \"decision\": \"approve\"}]}"
  expect 200
  json 'b["items"][0]["http_status"] == 200' >/dev/null || fail "the reviewer's approve was refused"
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

step errors_load
bash -c "$ERRORS_LOAD" errors_load "$error_map" >/dev/null || fail "the ERRORS_LOAD hook failed"

step login_acme_owner
owner="$(login owner@acme.test)"

step cleanup_live_amazon
call GET "$api/marketplaces/accounts?limit=200" "$owner"
expect 200
for stale in $(json '" ".join(a["id"] for a in b["data"] if a["marketplace"] == "amazon" and a["status"] != "revoked")'); do
  call POST "$api/marketplaces/accounts/$stale/revoke" "$owner"
  expect 200
done

step connect_confirm
account="$(connect_confirm "$owner")"

step login_acme_operator
operator="$(login operator@acme.test)"

step login_acme_reviewer
reviewer="$(login reviewer@acme.test)"

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

step create_generate_approve
generated_approved "$prefix-A"
first="$listing_id"
items="{\"items\": [{\"listing_id\": \"$first\", \"listing_version_id\": \"$version\"}]}"

step reviewer_publish_403
call POST "$api/listings/publish" "$reviewer" "e2e-pub-rev-$run" "$items"
expect 403

step publish_202
call POST "$api/listings/publish" "$operator" "e2e-pub-$run" "$items"
expect 202
json 'b["items"][0]["http_status"] == 202 and b["items"][0]["listing_status"] == "publishing"' >/dev/null
job="$(json 'b["items"][0]["job_id"]')"
cp "$body" "$work/publish.json"

step publish_replay_identical
call POST "$api/listings/publish" "$operator" "e2e-pub-$run" "$items"
expect 202
cmp -s "$body" "$work/publish.json" || fail "the replay answered another body"

step publish_other_body_409
call POST "$api/listings/publish" "$operator" "e2e-pub-$run" "{\"items\": [{\"listing_id\": \"$first\"}]}"
expect 409
json 'b["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"' >/dev/null

step poll_published_processing
wait_job "$operator" "$job" succeeded
wait_listing "$operator" "$first" 'b["status"] == "published" and b["marketplace_state"] == "processing" and b["live_url"] is None'

step fake_set_buyable
fake_control "{\"spapi_get\": {\"$prefix-A\": \"buyable\"}}"

step refresh_status_202
call POST "$api/listings/$first/refresh-status" "$operator" "e2e-pub-refresh-$run"
expect 202
sync_job="$(json 'b["job_id"]')"

step refresh_again_429
call POST "$api/listings/$first/refresh-status" "$operator" "e2e-pub-refresh2-$run"
[ "$status" = "429" ] || [ "$status" = "202" ] || fail "want HTTP 429 (or the live job)"
if [ "$status" = "202" ]; then
  [ "$(json 'b["job_id"]')" = "$sync_job" ] || fail "a second sync job inside 5 minutes"
fi

step poll_live
wait_job "$operator" "$sync_job" succeeded
call POST "$api/listings/$first/refresh-status" "$operator" "e2e-pub-refresh3-$run"
expect 429
wait_listing "$operator" "$first" 'b["status"] == "published" and b["marketplace_state"] == "live"'

step create_generate_approve_2
generated_approved "$prefix-B"
second="$listing_id"

step fake_fail_two_fields
fake_control "{\"spapi_issues\": {\"$prefix-B\": [
  {\"code\": \"FAKE-QUOTA\", \"message\": \"Request throttled\", \"severity\": \"ERROR\", \"attributeNames\": [\"item_name\"]},
  {\"code\": \"FAKE-QUOTA\", \"message\": \"Request throttled\", \"severity\": \"ERROR\", \"attributeNames\": [\"bullet_point\"]}]}}"

step publish_2
call POST "$api/listings/publish" "$operator" "e2e-pub-2-$run" "{\"items\": [{\"listing_id\": \"$second\"}]}"
expect 202
job="$(json 'b["items"][0]["job_id"]')"

step poll_failed_partial
wait_job "$operator" "$job" partial
wait_listing "$operator" "$second" 'b["status"] == "failed" and b["status_reason"] == "publish_partial"'

step submissions_2_fields_failed
call GET "$api/listings/$second/submissions" "$operator"
expect 200
json 'sorted(f["field"] for f in b["data"][0]["items"][0]["fields"] if f["status"] == "failed") == ["content.bullets", "content.title"] and b["data"][0]["outcome"] == "partially_accepted"' >/dev/null

step retry_fields_202
patches_before="$(curl -s --max-time "$bound" "$fake/__calls" | python3 -c 'import json,sys; print(sum(1 for c in json.load(sys.stdin) if c["method"] == "PATCH"))')"
call POST "$api/listings/$second/publish/retry-fields" "$operator" "e2e-pub-retry-$run"
expect 202
job="$(json 'b["job_id"]')"

step poll_published_2
wait_job "$operator" "$job" succeeded
wait_listing "$operator" "$second" 'b["status"] == "published"'

step fake_calls_only_two_paths
call GET "$api/listings/$second/submissions" "$operator"
expect 200
json 'b["data"][0]["operation"] == "patch" and b["data"][0]["field_paths"] == ["content.bullets", "content.title"] and b["data"][0]["outcome"] == "accepted" and b["data"][0]["retry_justification"] == "retryable_condition"' >/dev/null
patches_after="$(curl -s --max-time "$bound" "$fake/__calls" | python3 -c 'import json,sys; print(sum(1 for c in json.load(sys.stdin) if c["method"] == "PATCH"))')"
[ "$((patches_after - patches_before))" = "1" ] || fail "the retry did not send exactly one PATCH"

step no_token_401
call GET "$api/listings/$second/submissions"
expect 401

step login_bharat_operator
bharat="$(login operator@bharat.test)"

step bharat_publish_item_404
call POST "$api/listings/publish" "$bharat" "e2e-pub-bharat-$run" "{\"items\": [{\"listing_id\": \"$first\"}]}"
expect 202
json 'b["items"][0]["http_status"] == 404' >/dev/null

step bharat_submissions_404
call GET "$api/listings/$first/submissions" "$bharat"
expect 404

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

step revoke_account
call POST "$api/marketplaces/accounts/$account/revoke" "$owner"
expect 200

echo "e2e: publish_flow OK (listings $first, $second)"
