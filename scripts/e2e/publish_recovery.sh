#!/usr/bin/env bash
# The S21 publish recovery flow, HTTP only against the local fake marketplace (CODE-PLAN S21 T26;
# AC15): an acme owner connects Amazon (its own seller); a reviewer-approved listing is published
# while the fake refuses the token twice (AUTH_EXPIRED after the one forced refresh): the listing is
# `blocked/marketplace_action_required` and the submission `error AUTH_EXPIRED`; the owner
# reconnects the same seller (S15 applies the reconnect task), and a publish resumes
# (`resume_publish`, attempt 2 justified by `human_resolution`) to `published`. A second listing
# meets a fake that throttles every SP-API call: three rate-limited attempts block it
# (`blocked/publish_attempts_exhausted`), and both publish and retry-fields then refuse with 409
# SUBMISSION_ATTEMPTS_EXHAUSTED. The run's SKUs are reset on the fake and the account revoked.
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
prefix="E2E-REC-$run"
seller="E2EREC$run"
category="ethnic_wear_kurta_sets" # the seeded category with a current, mapped Amazon template
error_map="apps/api/app/db/seed_data/error_map.fake.json"
bound=60

work="$(mktemp -d)"
worker_running=0
cleanup() {
  curl -s -o /dev/null --max-time 10 -X POST -H 'Content-Type: application/json' \
    --data-binary '{"spapi_429": 0}' "$fake/__control" || true
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
  call POST "$api/products" "$operator" "" "{\"sku\": \"$1\", \"title\": \"E2E Recovery Kurta Set\", \"category\": \"$category\"}"
  expect 201
  local product job
  product="$(json 'b["id"]')"
  call POST "$api/listings/generate" "$operator" "e2e-rec-gen-$1" "{\"product_id\": \"$product\", \"marketplace\": \"amazon\"}"
  expect 202
  job="$(json 'b["job_id"]')"
  listing_id="$(json 'b["listing_id"]')"
  wait_job "$operator" "$job" succeeded
  call GET "$api/listings/$listing_id" "$operator"
  expect 200
  json 'b["status"] in ("validated", "needs_review")' >/dev/null || fail "the generation did not validate"
  version="$(json 'b["current_version_id"]')"
  call POST "$api/listings/approve" "$reviewer" "e2e-rec-approve-$1" \
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

step fake_auth_expired
fake_control '{"spapi_auth": {"mode": "expired", "times": 2}}'

step publish_202
call POST "$api/listings/publish" "$operator" "e2e-rec-$run" "{\"items\": [{\"listing_id\": \"$first\"}]}"
expect 202
job="$(json 'b["items"][0]["job_id"]')"

step poll_blocked_action_required
wait_job "$operator" "$job" failed
wait_listing "$operator" "$first" 'b["status"] == "blocked" and b["status_reason"] == "marketplace_action_required"'
call GET "$api/listings/$first/submissions" "$operator"
expect 200
json 'b["data"][0]["outcome"] == "error" and b["data"][0]["error_code"] == "AUTH_EXPIRED"' >/dev/null

step publish_blocked_until_reconnect_409
call POST "$api/listings/publish" "$operator" "e2e-rec-early-$run" "{\"items\": [{\"listing_id\": \"$first\"}]}"
expect 202
json 'b["items"][0]["http_status"] == 409 and b["items"][0]["error"]["error_code"] == "DUPLICATE_SUBMISSION_BLOCKED"' >/dev/null

step reconnect
reconnected="$(connect_confirm "$owner")"
[ "$reconnected" = "$account" ] || fail "the reconnect made another account"

step publish_resume_202
call POST "$api/listings/publish" "$operator" "e2e-rec-resume-$run" "{\"items\": [{\"listing_id\": \"$first\"}]}"
expect 202
json 'b["items"][0]["http_status"] == 202' >/dev/null
job="$(json 'b["items"][0]["job_id"]')"

step poll_published
wait_job "$operator" "$job" succeeded
wait_listing "$operator" "$first" 'b["status"] == "published" and b["marketplace_state"] == "processing"'
call GET "$api/listings/$first/submissions" "$operator"
expect 200
json 'b["data"][0]["attempt"] == 2 and b["data"][0]["outcome"] == "accepted" and b["data"][0]["retry_justification"] == "human_resolution"' >/dev/null

step create_generate_approve_2
generated_approved "$prefix-B"
second="$listing_id"

step fake_429_always
fake_control '{"spapi_429": 1000}'

step publish_until_exhausted
call POST "$api/listings/publish" "$operator" "e2e-rec-429-$run" "{\"items\": [{\"listing_id\": \"$second\"}]}"
expect 202
job="$(json 'b["items"][0]["job_id"]')"
wait_job "$operator" "$job" failed

step poll_blocked_attempts_exhausted
wait_listing "$operator" "$second" 'b["status"] == "blocked" and b["status_reason"] == "publish_attempts_exhausted"'
call GET "$api/listings/$second/submissions" "$operator"
expect 200
json '[s["error_code"] for s in b["data"]] == ["RATE_LIMITED", "RATE_LIMITED", "RATE_LIMITED"]' >/dev/null

step publish_cap_409
call POST "$api/listings/publish" "$operator" "e2e-rec-cap-$run" "{\"items\": [{\"listing_id\": \"$second\"}]}"
expect 202
json 'b["items"][0]["http_status"] == 409 and b["items"][0]["error"]["error_code"] == "SUBMISSION_ATTEMPTS_EXHAUSTED"' >/dev/null

step retry_fields_cap_409
call POST "$api/listings/$second/publish/retry-fields" "$operator" "e2e-rec-capr-$run"
expect 409
json 'b["error"]["type"] == "SUBMISSION_ATTEMPTS_EXHAUSTED"' >/dev/null

step fake_reset
fake_control '{"spapi_429": 0}'

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

step revoke_account
call POST "$api/marketplaces/accounts/$account/revoke" "$owner"
expect 200

echo "e2e: publish_recovery OK (listings $first, $second)"
