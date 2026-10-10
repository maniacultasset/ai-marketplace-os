#!/usr/bin/env bash
# The v1 acceptance run, HTTP only after setup (S26 T20-T21; AC 9, AC 3, DW3; PLAN D3-D5, D14):
# `make acceptance` runs it on a fresh stack (profile acceptance: its own compose project, its own
# fake marketplace, one worker, FLIPKART_ENABLED=false). `make e2e` skips it.
#
#   setup                  staff CLI only (D5): a run-unique tenant and its owner (`tenant create`),
#                          an operator and a reviewer (`tenant add-member`), the owner's password
#                          reset (`user set-password`), `platform load` (S20)
#   login_*                owner, operator, reviewer (passwords from files, never an argv)
#   connect_amazon         connect -> the fake's consent -> callback -> confirm; account active
#   import_csv             FSH-TEE-001 tshirts, "Jet Black", S/M/L/XL (4 rows imported)
#   brand_document_md      a .md brand guide -> its job succeeded, document ready
#   images_upload          a front (main) image per child -> each report passed
#   generate_stub          the owner generates (stub pipeline) -> succeeded; its version kept
#   validate_passed        POST /listings/validate -> the Rules Engine passes it
#   owner_self_approve_item_403         200 BatchResult, item 403 MAKER_CHECKER_VIOLATION
#   operator_approve_permission_denied  request 403 PERMISSION_DENIED (RBAC before the handler)
#   reviewer_approve_200   the same listing_version_id all three times (no STALE_VERSION)
#   preview                the Amazon payload preview (S20)
#   publish                the fake rejects the XL child once (FAKE-QUOTA, a mapped transient
#                          code) -> job partial, the listing names the failed XL field
#   retry_fields           only the failed field is resent (one PATCH, to the XL SKU)
#   submissions_attempt2   attempt 2: retry_justification retryable_condition, no corrections
#   retry_succeeded        job succeeded, listing published + processing ("Submitted")
#   remap_skip             SKIP T41 (BRD adapter remap: not in v1, PLAN D14)
#   status_sync            the fake reports every SKU BUYABLE; refresh-status 202 -> live + URL
#   generate_flipkart_stub / flipkart_publish_refused   flag off: item 501 NOT_IMPLEMENTED, no job,
#                          status unchanged, no submissions, 0 Flipkart calls at the fake
#   publish_replay         the first publish's key and body -> the stored 202 body
#   analytics              4 metrics equal the values this run's HTTP answers imply
#   ask_question_skip      SKIP C2 ("ask one question": not in v1)
#   no_canaries_in_logs    the stack's logs hold no password, issued JWT or refresh token
#
# Usage: FAKE_URL=<fake> MKOS_CLI=<cmd prefix> [STACK_LOGS=<dir>] scripts/e2e/acceptance.sh base_url
# MKOS_CLI runs `python -m app.cli` against the stack (its arguments appended; a new password on
# stdin). STACK_LOGS is the stack's log dir for the last leg (default: none, the leg fails).
# Local only: any other host exits 2 before a request; a missing hook exits 2. Every wait is
# bounded (60 s; 120 s for a job). Rerunnable: run-unique slug, emails, seller and keys. On
# failure it names the leg and the HTTP status seen, never a body.
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  echo "usage: FAKE_URL=<fake> MKOS_CLI=<cmd prefix> [STACK_LOGS=<dir>] $0 base_url" >&2
  exit 2
}
[ -n "${FAKE_URL:-}" ] && [ -n "${MKOS_CLI:-}" ] && [ $# -eq 1 ] || usage
base="$1"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
fake="${FAKE_URL%/}"
python3 scripts/ci/local_url.py "$fake" || exit 2
api="$base/api/v1"
run="$(date +%s)$RANDOM"
slug="acc-$run"
owner_email="owner-$slug@example.test"
operator_email="operator-$slug@example.test"
reviewer_email="reviewer-$slug@example.test"
staff_email="staff@example.test"
seller="ACC$run"
style="FSH-TEE-001"
sizes="S M L XL"
guide="scripts/e2e/fixtures/brand_guide.md"
bound=60
job_bound=120

work="$(mktemp -d)"
chmod 0700 "$work"
cleanup() {
  curl -s -o /dev/null --max-time 10 -X POST -H 'Content-Type: application/json' \
    --data-binary "{\"prefix\": \"$style\"}" "$fake/__control/reset" || true
  rm -rf "$work"
}
trap cleanup EXIT
body="$work/body.json"
current="setup"
status=""
location=""

leg() {
  current="$1"
  echo "acceptance: leg $current"
}

fail() {
  echo "acceptance: FAILED at leg $current (HTTP status ${status:-none}): $1" >&2
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

# multipart TOKEN KEY URL FORM...: a multipart POST; status in $status, body in $body.
multipart() {
  local token="$1" key="$2" url="$3"
  shift 3
  local args=(-s -o "$body" -w '%{http_code}' --max-time "$bound" -X POST -H 'Accept: application/json'
    -H "Authorization: Bearer $token" -H "Idempotency-Key: $key")
  local part
  for part in "$@"; do args+=(-F "$part"); done
  status="$(curl "${args[@]}" "$url")" || status="000"
}

# json EXPR: evaluate a Python expression over the parsed body `b`; prints the result; a false
# or failing expression fails the leg. `holds EXPR` is the same test without failing the leg.
json() {
  evaluate "$1" || fail "unexpected response shape"
}

holds() {
  evaluate "$1" >/dev/null 2>&1
}

evaluate() {
  python3 - "$body" "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        b = json.load(fh)
    scope = {"__builtins__": {}, "b": b, "len": len, "str": str, "all": all, "any": any,
             "set": set, "sorted": sorted, "sum": sum, "round": round, "abs": abs,
             "float": float, "int": int, "isinstance": isinstance, "dict": dict, "list": list}
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

new_password() {
  python3 -c 'import secrets; print("CANARYpw-" + secrets.token_hex(12))'
}

# cli PASSWORD_FILE ARGS...: the staff CLI with the password file as its stdin; exit in $cli_exit.
cli() {
  local secret_file="$1"
  shift
  cli_exit=0
  bash -c "$MKOS_CLI"' "$@"' mkos "$@" <"$secret_file" >"$work/cli.out" 2>"$work/cli.err" \
    || cli_exit=$?
}

# login EMAIL PASSWORD_FILE: the body is written to a file, so the password is on no argv; the
# access and refresh tokens are kept (for the log canary leg) in 0600 files.
login() {
  local email="$1" secret_file="$2"
  python3 -c '
import json, sys
with open(sys.argv[2], encoding="utf-8") as fh:
    secret = fh.readline().rstrip("\n")
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump({"email": sys.argv[3], "password": secret}, fh)
' "$work/login.json" "$secret_file" "$email"
  status="$(curl -s -o "$body" -w '%{http_code}' --max-time "$bound" -X POST \
    -H 'Content-Type: application/json' --data-binary "@$work/login.json" "$api/auth/login")" \
    || status="000"
  rm -f "$work/login.json"
  expect 200
  json 'b["refresh_token"]' >>"$work/canaries"
  json 'b["access_token"]' | tee -a "$work/canaries"
}

# wait_job TOKEN JOB STATUS [BOUND]: poll the job envelope (1 s) until STATUS.
wait_job() {
  local deadline=$(( $(date +%s) + ${4:-$job_bound} )) seen
  while :; do
    call GET "$api/jobs/$2" "$1"
    expect 200
    seen="$(json 'b["status"]')"
    if [ "$seen" = "$3" ]; then return 0; fi
    case "$seen" in succeeded|partial|failed|cancelled|dead_letter) fail "the job ended $seen, want $3" ;; esac
    if [ "$(date +%s)" -ge "$deadline" ]; then fail "the job did not reach $3 in time"; fi
    sleep 1
  done
}

# wait_listing TOKEN LISTING EXPR: poll the listing (1 s) until EXPR holds.
wait_listing() {
  local deadline=$(( $(date +%s) + bound ))
  while :; do
    call GET "$api/listings/$2" "$1"
    expect 200
    if holds "$3"; then return 0; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then fail "the listing did not reach the state in time"; fi
    sleep 1
  done
}

fake_control() {
  call POST "$fake/__control" "" "" "$1"
  expect 200
}

# fake_patches SKU_PREFIX: the fake's count of SP-API PATCH calls for SKUs under the prefix.
fake_patches() {
  curl -s --max-time "$bound" "$fake/__calls" | python3 -c '
import json, sys
calls = json.load(sys.stdin)
calls = calls["calls"] if isinstance(calls, dict) else calls
print(sum(1 for c in calls if c["method"] == "PATCH" and ("/" + sys.argv[1]) in c["path"]))
' "$1"
}

# ---------------------------------------------------------------------------------------------
leg setup
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$bound" "$base/readyz")" || status="000"
expect 200
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$bound" "$fake/healthz")" || status="000"
expect 200
umask 077
: >"$work/canaries"
for who in owner operator reviewer owner_reset; do
  new_password >"$work/$who.password"
  cat "$work/$who.password" >>"$work/canaries"
done
cli /dev/null platform load
[ "$cli_exit" -eq 0 ] || fail "platform load exited $cli_exit"
cli "$work/owner.password" tenant create --name "Acceptance $slug" --slug "$slug" \
  --owner-email "$owner_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant create exited $cli_exit"
cli "$work/operator.password" tenant add-member --slug "$slug" --email "$operator_email" \
  --role operator --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant add-member operator exited $cli_exit"
cli "$work/reviewer.password" tenant add-member --slug "$slug" --email "$reviewer_email" \
  --role reviewer --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant add-member reviewer exited $cli_exit"
cli "$work/owner_reset.password" user set-password --email "$owner_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "user set-password exited $cli_exit"

leg login_owner
owner="$(login "$owner_email" "$work/owner_reset.password")"

leg login_operator
operator="$(login "$operator_email" "$work/operator.password")"

leg login_reviewer
reviewer="$(login "$reviewer_email" "$work/reviewer.password")"

leg connect_amazon
call POST "$api/marketplaces/connect" "$owner" "" '{"marketplace": "amazon"}'
expect 200
authorize="$(json 'b["authorize_url"]')"
case "$authorize" in "$fake"/*) ;; *) fail "the consent URL is not the fake's" ;; esac
call GET "$authorize&seller=$seller"
expect 302
call GET "$base$(path_query "$location")"
expect 303
pending="$(query "$location" pending)"
[ -n "$pending" ] || fail "no pending handle"
call POST "$api/marketplaces/connect/confirm" "$owner" "" "{\"pending\": \"$pending\"}"
expect 200
account="$(json 'b["id"]')"
call GET "$api/marketplaces/accounts" "$owner"
expect 200
json 'any(a["id"] == "'"$account"'" and a["status"] == "active" and a["marketplace"] == "amazon" for a in b["data"])' >/dev/null \
  || fail "the Amazon account is not active"

leg import_csv
{
  echo "seller_sku,parent_style_id,product_name,brand_name,category,gender,color_name,normalized_color,size_name,fabric_type,mrp,selling_price,tax_rate,hsn_code,country_of_origin,package_weight,main_image"
  for size in $sizes; do
    echo "$style-$size,$style,Women's Relaxed Fit Cotton T-Shirt,Fashionista,tshirts,women,Jet Black,black,$size,Cotton,999,799,5,61091000,IN,180 g,https://cdn.example.com/acc/$style-$size.jpg"
  done
} >"$work/products.csv"
multipart "$operator" "acc-import-$run" "$api/products/bulk" "file=@$work/products.csv;type=text/csv"
expect 202
import_id="$(json 'b["import_id"]')"
wait_job "$operator" "$(json 'b["job_id"]')" succeeded
call GET "$api/imports/$import_id" "$operator"
expect 200
json 'b["imported"] == 4 and b["failed"] == 0 and b["total_rows"] == 4' >/dev/null || fail "want 4 rows imported, 0 failed"
call GET "$api/imports/$import_id/rows?status=imported&limit=10" "$operator"
expect 200
python3 - "$body" "$work/children" <<'PY' || fail "the imported rows are not the four children"
import json, sys
rows = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
skus = {r["sku"]: r["product_id"] for r in rows}
want = [f"FSH-TEE-001-{s}" for s in ("S", "M", "L", "XL")]
assert sorted(skus) == sorted(want) and all(skus.values())
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    for sku in want:
        fh.write(f"{sku} {skus[sku]}\n")
PY
call GET "$api/products?limit=50" "$operator"
expect 200
style_id="$(json '[p["id"] for p in b["data"] if p["sku"] == "'"$style"'"][0]')"
[ -n "$style_id" ] || fail "no style product $style"

leg brand_document_md
multipart "$owner" "acc-brand-$run" "$api/brand/documents" "file=@$guide;type=text/markdown"
expect 202
wait_job "$owner" "$(json 'b["job_id"]')" succeeded
call GET "$api/brand/documents" "$owner"
expect 200
json 'len(b["data"]) == 1 and b["data"][0]["status"] == "ready" and b["data"][0]["chunk_count"] > 0' >/dev/null \
  || fail "the brand document is not ready"

leg images_upload
uv run --frozen python scripts/e2e/make_image.py 1000 1000 255,255,255 PNG "$work/front.png"
while read -r sku product; do
  multipart "$operator" "acc-img-$run-$sku" "$api/images/upload" "product_id=$product" "slot=main" \
    "file=@$work/front.png;type=application/octet-stream"
  expect 202
  image="$(json 'b["image_id"]')"
  wait_job "$operator" "$(json 'b["job_id"]')" succeeded
  call GET "$api/images/$image/report" "$operator"
  expect 200
  json 'b["compliance_status"] == "passed"' >/dev/null || fail "the $sku front image did not pass"
done <"$work/children"

leg generate_stub
call POST "$api/listings/generate" "$owner" "acc-gen-$run" "{\"product_id\": \"$style_id\", \"marketplace\": \"amazon\"}"
expect 202
listing_id="$(json 'b["listing_id"]')"
wait_job "$owner" "$(json 'b["job_id"]')" succeeded
call GET "$api/listings/$listing_id" "$owner"
expect 200
version="$(json 'b["current_version_id"]')"
[ -n "$version" ] || fail "no current version"
before_status="$(json 'b["status"]')"

leg validate_passed
call POST "$api/listings/validate" "$operator" "" "{\"listing_id\": \"$listing_id\", \"listing_version_id\": \"$version\"}"
expect 200
json 'b["passed"] is True and b["blocking_count"] == 0 and b["listing_version_id"] == "'"$version"'"' >/dev/null \
  || fail "the Rules Engine did not pass the listing"

decision="{\"listing_id\": \"$listing_id\", \"listing_version_id\": \"$version\", \"decision\": \"approve\"}"

leg owner_self_approve_item_403
call POST "$api/listings/approve" "$owner" "acc-approve-owner-$run" "{\"items\": [$decision]}"
expect 200
json 'b["items"][0]["http_status"] == 403 and b["items"][0]["error"]["errors"][0]["error_code"] == "MAKER_CHECKER_VIOLATION"' >/dev/null \
  || fail "want item 403 MAKER_CHECKER_VIOLATION"
call GET "$api/listings/$listing_id" "$owner"
expect 200
json 'b["status"] == "'"$before_status"'" and b["current_version_id"] == "'"$version"'"' >/dev/null \
  || fail "the self-approval changed the listing"
call GET "$api/listings/$listing_id/history" "$owner"
expect 200
json '"listing.approve_denied" in [e["action"] for e in b["data"]]' >/dev/null || fail "no listing.approve_denied in the history"

leg operator_approve_permission_denied
call POST "$api/listings/approve" "$operator" "acc-approve-op-$run" "{\"items\": [$decision]}"
expect 403
json 'b["error"]["type"] == "PERMISSION_DENIED"' >/dev/null || fail "want PERMISSION_DENIED"
call GET "$api/listings/$listing_id" "$operator"
expect 200
json 'b["status"] == "'"$before_status"'"' >/dev/null || fail "the operator's request changed the listing"

leg reviewer_approve_200
call POST "$api/listings/approve" "$reviewer" "acc-approve-$run" "{\"items\": [$decision]}"
expect 200
json 'b["items"][0]["http_status"] == 200 and b["items"][0]["listing_status"] == "approved"' >/dev/null \
  || fail "the reviewer's approve was refused"
call GET "$api/listings/$listing_id" "$reviewer"
expect 200
json 'b["status"] == "approved" and b["approved_version_id"] == "'"$version"'"' >/dev/null || fail "not approved at the version"

leg preview
call GET "$api/listings/$listing_id/preview" "$operator"
expect 200
json 'b["marketplace"] == "amazon" and len(b["items"]) == 5 and sorted(i["seller_sku"] for i in b["items"] if i["item_role"] == "child") == sorted(["'"$style"'-" + s for s in ("S", "M", "L", "XL")])' >/dev/null \
  || fail "the preview is not the style and its four children"

leg publish
fake_control "{\"spapi_issues\": {\"$style-XL\": [{\"code\": \"FAKE-QUOTA\", \"message\": \"Request throttled\", \"severity\": \"ERROR\", \"attributeNames\": [\"size\"]}]}}"
publish_body="{\"items\": [{\"listing_id\": \"$listing_id\", \"listing_version_id\": \"$version\"}]}"
call POST "$api/listings/publish" "$operator" "acc-publish-$run" "$publish_body"
expect 202
json 'b["items"][0]["http_status"] == 202' >/dev/null || fail "the publish item was refused"
cp "$body" "$work/publish.json"
publish_job="$(json 'b["items"][0]["job_id"]')"
wait_job "$operator" "$publish_job" partial
wait_listing "$operator" "$listing_id" 'b["status"] == "failed" and b["status_reason"] == "publish_partial"'
call GET "$api/listings/$listing_id/status" "$operator"
expect 200
json 'b["status"] == "failed" and b["status_reason"] == "publish_partial"' >/dev/null || fail "the status is not publish_partial"
call GET "$api/listings/$listing_id/submissions" "$operator"
expect 200
failed_fields="$(json '" ".join(sorted({f["field"] for s in b["data"] for i in s["items"] if i["seller_sku"] == "'"$style"'-XL" for f in i["fields"] if f["status"] == "failed"}))')"
[ -n "$failed_fields" ] || fail "no failed XL field in the submissions"
case "$failed_fields" in *size*) ;; *) fail "the failed XL field is not the size ($failed_fields)" ;; esac
json 'all(f["status"] != "failed" for s in b["data"] for i in s["items"] if i["seller_sku"] != "'"$style"'-XL" for f in i["fields"])' >/dev/null \
  || fail "a field other than XL's failed"

leg retry_fields
patches_before="$(fake_patches "$style")"
xl_before="$(fake_patches "$style-XL")"
call POST "$api/listings/$listing_id/publish/retry-fields" "$operator" "acc-retry-$run"
expect 202
retry_job="$(json 'b["job_id"]')"
wait_job "$operator" "$retry_job" succeeded
patches_after="$(fake_patches "$style")"
xl_after="$(fake_patches "$style-XL")"
[ "$((patches_after - patches_before))" = "1" ] && [ "$((xl_after - xl_before))" = "1" ] \
  || fail "the retry did not send exactly one PATCH, to the XL SKU"

leg submissions_attempt2
call GET "$api/listings/$listing_id/submissions" "$operator"
expect 200
json 'b["data"][0]["attempt"] == 2 and b["data"][0]["operation"] == "patch" and b["data"][0]["retry_justification"] == "retryable_condition" and b["data"][0].get("corrections_json") is None and b["data"][0]["outcome"] == "accepted"' >/dev/null \
  || fail "attempt 2 is not a retryable_condition PATCH without corrections"
json 'sorted({i["seller_sku"] for i in b["data"][0]["items"]}) == ["'"$style"'-XL"]' >/dev/null \
  || fail "attempt 2 touched another SKU"

leg retry_succeeded
wait_listing "$operator" "$listing_id" 'b["status"] == "published" and b["marketplace_state"] == "processing" and b["live_url"] is None'

leg remap_skip
echo "acceptance: SKIP T41 (the BRD adapter remap of a rejected field is not in v1; PLAN D14, DATABASE.md:1798)"

leg status_sync
gets="$(python3 -c 'import json, sys; print(json.dumps({"spapi_get": {f"FSH-TEE-001{s}": "buyable" for s in ("", "-S", "-M", "-L", "-XL")}}))')"
fake_control "$gets"
call POST "$api/listings/$listing_id/refresh-status" "$operator" "acc-refresh-$run"
expect 202
sync_job="$(json 'b["job_id"]')"
wait_job "$operator" "$sync_job" succeeded
wait_listing "$operator" "$listing_id" 'b["status"] == "published" and b["marketplace_state"] == "live" and b["id"] == "'"$listing_id"'" and isinstance(b["live_url"], str) and b["live_url"].startswith("http")'

leg generate_flipkart_stub
call POST "$api/listings/generate" "$owner" "acc-gen-fk-$run" "{\"product_id\": \"$style_id\", \"marketplace\": \"flipkart\"}"
expect 202
fk_listing="$(json 'b["listing_id"]')"
wait_job "$owner" "$(json 'b["job_id"]')" succeeded
call GET "$api/listings/$fk_listing" "$owner"
expect 200
fk_status="$(json 'b["status"]')"

leg flipkart_publish_refused
fk_before="$(curl -s --max-time "$bound" "$fake/__calls?marketplace=flipkart" | python3 -c 'import json, sys; print(json.load(sys.stdin)["count"])')"
call POST "$api/listings/publish" "$operator" "acc-publish-fk-$run" "{\"items\": [{\"listing_id\": \"$fk_listing\"}]}"
expect 202
json 'b["items"][0]["http_status"] == 501 and b["items"][0]["error"]["errors"][0]["error_code"] == "NOT_IMPLEMENTED" and b["items"][0]["job_id"] is None' >/dev/null \
  || fail "want item 501 NOT_IMPLEMENTED with no job"
call GET "$api/listings/$fk_listing" "$operator"
expect 200
json 'b["status"] == "'"$fk_status"'"' >/dev/null || fail "the refused publish changed the listing"
call GET "$api/listings/$fk_listing/submissions" "$operator"
expect 200
json 'b["data"] == []' >/dev/null || fail "the refused publish wrote a submission"
fk_after="$(curl -s --max-time "$bound" "$fake/__calls?marketplace=flipkart" | python3 -c 'import json, sys; print(json.load(sys.stdin)["count"])')"
[ "$fk_before" = "$fk_after" ] || fail "the refused publish called the Flipkart fake"

leg publish_replay
call POST "$api/listings/publish" "$operator" "acc-publish-$run" "$publish_body"
expect 202
cmp -s "$body" "$work/publish.json" || fail "the replay did not answer the stored body"

leg analytics
call GET "$api/listings/$listing_id/submissions" "$operator"
expect 200
outcomes="$(json '",".join(s["outcome"] for s in b["data"])')"
range="from=$(date -u -v-1d +%Y-%m-%d 2>/dev/null || date -u -d yesterday +%Y-%m-%d)&to=$(date -u -v+1d +%Y-%m-%d 2>/dev/null || date -u -d tomorrow +%Y-%m-%d)"
call GET "$api/analytics?$range" "$owner"
expect 200
python3 - "$body" "$outcomes" <<'PY' || fail "the analytics do not match this run"
import json, sys
from decimal import ROUND_HALF_EVEN, Decimal

b = json.load(open(sys.argv[1], encoding="utf-8"))
outcomes = [o for o in sys.argv[2].split(",") if o]
c = b["counts"]

def rate(num, den):
    if den == 0:
        return None
    return float((Decimal(num) / Decimal(den)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN))

succeeded = sum(1 for o in outcomes if o in ("accepted", "live", "superseded"))
failed = sum(1 for o in outcomes if o in ("partially_accepted", "rejected"))
# this run: two generations (amazon, flipkart), one approval, no rejection, two submissions
assert b["listings_generated"] == 2 == c["listings_generated"]
assert (c["approvals"], c["rejections"]) == (1, 0) and b["approval_rate"] == 1.0
assert (c["publish_succeeded"], c["publish_failed"]) == (succeeded, failed) == (1, 1)
assert b["publish_success_rate"] == rate(succeeded, succeeded + failed) == 0.5
assert c["validation_first_pass_total"] >= 2
assert b["validation_pass_rate"] == rate(c["validation_first_pass_passed"], c["validation_first_pass_total"])
PY

leg ask_question_skip
echo "acceptance: SKIP C2 (\"ask one question\" is not in v1)"

leg no_canaries_in_logs
[ -n "${STACK_LOGS:-}" ] && [ -d "$STACK_LOGS" ] || fail "STACK_LOGS is not the stack's log dir"
[ "$(wc -l <"$work/canaries")" -ge 10 ] || fail "too few canaries collected"
if grep -F -r -q -f "$work/canaries" "$STACK_LOGS"; then
  fail "a password, JWT or refresh token appears in the stack's logs"
fi

echo "acceptance: OK (tenant $slug, listing $listing_id)"
