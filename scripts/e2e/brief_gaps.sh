#!/usr/bin/env bash
# SI13 (CODE-PLAN T18; P:AC20; CP-D14), HTTP only: the team-brief gaps over the fake marketplace.
# - category_code: Amazon's dry run answers 4000003; the issue reads lane `seller` at
#   `facts.category`; the seller recategorises to tshirts, regenerates, a teammate approves, the
#   publish sends a PUT with the new product type (SHIRT).
# - other_brand_ean: the catalog pre-check finds the barcode under another brand; approve is
#   refused.
# - brand_restricted: the barcode's ASIN needs approval; a `permission_decision` task carries the
#   Seller Central link.
# - unknown_code: an unmapped code reads lane `staff`, and no AI proposal text reaches the seller.
# The child-axis repair (`child_missing_axis`) is not here: a family needs the import flow; it is
# proved by the unit suite (`tests/unit/resolution/test_repairs.py`). Each product carries a saved
# size chart and a front, back and close-up photo before it generates, so the photo view
# checklist (`PHOTO_VIEWS_ENABLED`) is met: the approve after the recategorise passes it (T-shirts
# require the chart too), and the other-brand barcode's refusal stays the catalog check's.
#
# Usage: FAKE_URL=<fake> WORKER_START=<cmd> WORKER_STOP=<cmd> ERRORS_LOAD=<cmd> \
#   scripts/e2e/brief_gaps.sh [base_url]
# Needs AMAZON_VALIDATION_PREVIEW_ENABLED and AMAZON_CATALOG_PRECHECK_ENABLED (`.env.example`
# turns both on). Local only (the demo seed's public password): any other host exits 2 before a
# request. A SKU prefix per run, reset at the end. On failure it prints the step name and the HTTP
# status, never a body.
set -euo pipefail
cd "$(dirname "$0")/../.."
. scripts/e2e/lib/photo_views.sh

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
prefix="E2E-GAP-$run"
seller="E2EGAP$run"
category="ethnic_wear_kurta_sets" # the seeded category with a current, mapped Amazon template
error_map="apps/api/app/db/seed_data/error_map.amazon.json" # SI13: the published code rows
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

# generated SKU [IDENTIFIER_JSON]: a product (with its size chart and photos) and a stub
# generation; sets $product, $listing_id and $version once the generation and its dry run settled.
generated() {
  local ident="${2:-\"identifier_exemption\": true}" job
  call POST "$api/products" "$operator" "" "{\"sku\": \"$1\", \"title\": \"E2E Gaps Kurta Set\", \"category\": \"$category\", \"mrp\": \"1499.00\", \"selling_price\": \"999.00\", \"gender\": \"women\", \"size_chart\": $PHOTO_VIEWS_CHART, $ident}"
  expect 201
  product="$(json 'b["id"]')"
  photo_views_upload "$api" "$operator" "$product" "e2e-gap-img-$1" "$work" || fail "the photo checklist is not met"
  call POST "$api/listings/generate" "$operator" "e2e-gap-gen-$1" "{\"product_id\": \"$product\", \"marketplace\": \"amazon\"}"
  expect 202
  job="$(json 'b["job_id"]')"
  listing_id="$(json 'b["listing_id"]')"
  wait_job "$operator" "$job" succeeded
  wait_listing "$operator" "$listing_id" '(b.get("remote_check") or {}).get("status") in ("clean", "issues", "failed")'
  version="$(json 'b["current_version_id"]')"
}

# issues_of LISTING: the listing's open ERROR issues (as the seller sees them) in $body.
issues_of() {
  call GET "$api/issues?listing_id=$1&status=open" "$operator"
  expect 200
}

# ean13 N: a valid EAN-13 for this run (890 prefix, the run digits, the GS1 check digit).
ean13() {
  python3 - "$run" "$1" <<'PY'
import sys
body = ("890" + (sys.argv[1] + sys.argv[2])[-9:]).ljust(12, "0")[:12]
total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body))
print(body + str((10 - total % 10) % 10))
PY
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

step category_code_seller_lane
fake_control "{\"spapi_scenario\": {\"$prefix-CAT\": \"category_code\"}}"
generated "$prefix-CAT"
cat_listing="$listing_id"
cat_product="$product"
issues_of "$cat_listing"
json 'any(i["field_path"] == "facts.category" and i["lane"] == "seller" and i["severity"] == "error" for i in b["data"])' >/dev/null

step category_next_step_recategorize
call GET "$api/listings/$cat_listing/resolution" "$operator"
expect 200
json 'b["next_step"]["kind"] == "recategorize"' >/dev/null

step recategorize_200
call POST "$api/products/$cat_product/recategorize" "$operator" "" '{"category": "tshirts"}'
expect 200

step regenerate_and_approve
call POST "$api/listings/$cat_listing/regenerate" "$operator" "e2e-gap-regen-$run"
case "$status" in 200|202) ;; *) fail "want HTTP 200 or 202" ;; esac
job="$(json 'b["job_id"]')"
wait_job "$operator" "$job" succeeded
wait_listing "$operator" "$cat_listing" '(b.get("remote_check") or {}).get("status") in ("clean", "issues", "failed") and b["status"] in ("validated", "needs_review")'
version="$(json 'b["current_version_id"]')"
call POST "$api/listings/approve" "$reviewer" "e2e-gap-approve-$run" \
  "{\"items\": [{\"listing_id\": \"$cat_listing\", \"listing_version_id\": \"$version\", \"decision\": \"approve\"}]}"
expect 200
json 'b["items"][0]["http_status"] == 200' >/dev/null || fail "the reviewer's approve was refused"

step publish_new_type
call POST "$api/listings/publish" "$operator" "e2e-gap-pub-$run" \
  "{\"items\": [{\"listing_id\": \"$cat_listing\", \"listing_version_id\": \"$version\"}]}"
expect 202
job="$(json 'b["items"][0]["job_id"]')"
wait_job "$operator" "$job" succeeded
call GET "$fake/__calls"
expect 200
json 'any(c["method"] == "PUT" and c["path"].endswith("/'"$prefix"'-CAT") for c in b)' >/dev/null

step other_brand_ean_blocks
other_ean="$(ean13 1)"
fake_control "{\"spapi_catalog\": {\"$other_ean\": {\"scenario\": \"other_brand_ean\"}}}"
generated "$prefix-EAN" "\"product_identifier\": {\"type\": \"ean\", \"value\": \"$other_ean\"}"
issues_of "$listing_id"
json 'any(i["field_path"] == "facts.identifier" and i["lane"] == "seller" and i["marketplace_code"] == "catalog:other_brand" for i in b["data"])' >/dev/null
call POST "$api/listings/approve" "$reviewer" "e2e-gap-approve-ean-$run" \
  "{\"items\": [{\"listing_id\": \"$listing_id\", \"listing_version_id\": \"$version\", \"decision\": \"approve\"}]}"
expect 200
json 'b["items"][0]["http_status"] in (409, 422)' >/dev/null || fail "approve was not refused"

step brand_restricted_task_link
restricted_ean="$(ean13 2)"
fake_control "{\"spapi_catalog\": {\"$restricted_ean\": {\"scenario\": \"same_brand_ean\", \"brand\": \"Acme Apparel\", \"restriction\": \"brand_restricted\"}}}"
generated "$prefix-RES" "\"product_identifier\": {\"type\": \"ean\", \"value\": \"$restricted_ean\"}"
call GET "$api/tasks?status=open&limit=200" "$owner"
expect 200
json 'any(t["kind"] == "permission_decision" and "sellercentral.amazon.in/hz/approvalrequest" in t["question"] for t in b["data"])' >/dev/null

step unknown_code_staff_lane
fake_control "{\"spapi_scenario\": {\"$prefix-UNK\": \"unknown_code\"}}"
generated "$prefix-UNK"
issues_of "$listing_id"
json 'any(i["marketplace_code"] == "77777" and i["lane"] == "staff" for i in b["data"])' >/dev/null
call GET "$api/listings/$listing_id" "$operator"
expect 200
json '"proposal" not in str(b) and "plain_cause" not in str(b)' >/dev/null

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

step revoke_account
call POST "$api/marketplaces/accounts/$account/revoke" "$owner"
expect 200

echo "e2e: brief_gaps OK (listing $cat_listing)"
