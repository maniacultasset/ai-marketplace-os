#!/usr/bin/env bash
# SI3 photo facts, HTTP only (CODE-PLAN SI3 T18; AC16): an acme operator uploads three product
# photos for a new standalone kurta set, generates the Amazon listing with the stub's
# `photo_mix` scenario (confident fills, an unclear colour), sees the photo values on the product
# and one colour question in the Inbox, generates the Flipkart listing (still one question, the
# same photo values), answers the question with `maroon` and reads the colour back as the seller's.
#
# "No second vision call" is not visible over HTTP (`PhotoFactOut` hides the run id); the
# integration suite proves it (`tests/integration/test_photo_generation.py`, AC3).
#
# Usage: WORKER_START=<cmd> WORKER_STOP=<cmd> TEMPLATES_LOAD=<cmd> scripts/e2e/photo_facts.sh [base_url]
# TEMPLATES_LOAD gets the seeded platform templates file as "$1" (the kurta templates list the
# visual keys the photo step reads). Local only.
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
run="$(date +%s)-$RANDOM"
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

# upload TOKEN KEY PRODUCT SLOT (file=PATH | storage_key=KEY): the multipart image upload.
upload() {
  local token="$1" key="$2" product="$3" slot="$4" part="$5"
  local args=(-s -o "$body" -w '%{http_code}' -X POST -H 'Accept: application/json'
    -H "Authorization: Bearer $token" -H "Idempotency-Key: $key"
    -F "product_id=$product" -F "slot=$slot")
  case "$part" in
    file=*) args+=(-F "file=@${part#file=};type=application/octet-stream") ;;
    storage_key=*) args+=(-F "$part") ;;
  esac
  status="$(curl "${args[@]}" "$api/images/upload")" || status="000"
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

# put_signed URL CONTENT_TYPE FILE [extra curl args...]: a PUT to a local signed URL only.
put_signed() {
  local url="$1" ctype="$2" file="$3"
  shift 3
  python3 -c 'import sys; from urllib.parse import urlsplit; u = urlsplit(sys.argv[1]); sys.exit(0 if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost") and not u.username else 2)' "$url" \
    || { echo "e2e: the signed URL is not on 127.0.0.1 or localhost" >&2; exit 2; }
  status="$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "Content-Type: $ctype" \
    -H 'If-None-Match: *' "$@" --data-binary "@$file" "$url")" || status="000"
}

image() { uv run --frozen python scripts/e2e/make_image.py "$@"; }
headers_file="$work/headers.txt"

# call_if METHOD PATH TOKEN IF_MATCH JSON: like `call`, with If-Match, keeping the response headers.
call_if() {
  local method="$1" path="$2" token="$3" tag="$4" data="$5"
  status="$(curl -s -o "$body" -D "$headers_file" -w '%{http_code}' -X "$method" \
    -H 'Accept: application/json' -H "Authorization: Bearer $token" -H "If-Match: $tag" \
    -H 'Content-Type: application/json' --data-binary "$data" "$api$path")" || status="000"
}

# etag_of METHOD PATH TOKEN: GET with headers kept; prints the ETag.
etag_of() {
  status="$(curl -s -o "$body" -D "$headers_file" -w '%{http_code}' -X GET \
    -H 'Accept: application/json' -H "Authorization: Bearer $3" "$api$2")" || status="000"
  expect 200
  tr -d '\r' <"$headers_file" | sed -n 's/^[Ee][Tt][Aa][Gg]: //p' | tail -n 1
}

etag_now() { tr -d '\r' <"$headers_file" | sed -n 's/^[Ee][Tt][Aa][Gg]: //p' | tail -n 1; }

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step templates_load
bash -c "$TEMPLATES_LOAD" templates_load apps/api/app/db/seed_data/platform_templates.json \
  || fail "the TEMPLATES_LOAD hook failed"

step login_acme_operator
operator="$(login operator@acme.test)"

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

step create_product_201
sku="E2E-SI3-$run"
call POST /products "$operator" "" "{\"sku\": \"$sku\", \"title\": \"E2E Photo Kurta Set\", \"category\": \"ethnic_wear_kurta_sets\", \"mrp\": \"1499.00\", \"attributes\": {\"fabric_type\": \"cotton\"}}"
expect 201
product="$(json 'b["id"]')"

n=0
for slot in main secondary lifestyle; do
  n=$((n + 1))
  image 1200 1200 "$((40 * n)),20,30" JPEG "$work/$slot.jpg"
  size="$(wc -c <"$work/$slot.jpg" | tr -d ' ')"
  step "upload_url_$slot"
  call POST /images/upload-url "$operator" "" "{\"product_id\": \"$product\", \"slot\": \"$slot\", \"content_type\": \"image/jpeg\", \"bytes\": $size}"
  expect 200
  url="$(json 'b["upload_url"]')"
  storage_key="$(json 'b["storage_key"]')"
  step "put_signed_$slot"
  put_signed "$url" image/jpeg "$work/$slot.jpg"
  expect 200
  step "register_$slot"
  upload "$operator" "e2e-si3-img-$slot-$run" "$product" "$slot" "storage_key=$storage_key"
  expect 202
  image_job="$(json 'b["job_id"]')"
  step "image_job_$slot"
  wait_job "$operator" "$image_job" succeeded
done

step generate_amazon_photo_mix
call POST /listings/generate "$operator" "e2e-si3-gen-a-$run" "{\"product_id\": \"$product\", \"marketplace\": \"amazon\", \"user_instructions\": \"stub:photo_mix\"}"
expect 202
wait_job "$operator" "$(json 'b["job_id"]')" succeeded

step photo_facts_listed
call GET "/products/$product" "$operator"
expect 200
json 'len(b["photo_facts"]) > 0 and all(f["confirmed"] is False for f in b["photo_facts"])' >/dev/null
json '"color" not in [f["key"] for f in b["photo_facts"]]' >/dev/null
facts_before="$(json '[(f["key"], str(f["value"])) for f in b["photo_facts"]]')"

step one_colour_question
call GET "/tasks" "$operator"
expect 200
task="$(json '[t for t in b["data"] if t["product_id"] == "'"$product"'"][0]["id"]')"
json 'len([t for t in b["data"] if t["product_id"] == "'"$product"'"]) == 1' >/dev/null
json '[t for t in b["data"] if t["product_id"] == "'"$product"'"][0]["target_path"] == "facts.attributes.color"' >/dev/null

step generate_flipkart
call POST /listings/generate "$operator" "e2e-si3-gen-f-$run" "{\"product_id\": \"$product\", \"marketplace\": \"flipkart\", \"user_instructions\": \"stub:photo_mix\"}"
expect 202
wait_job "$operator" "$(json 'b["job_id"]')" succeeded

step still_one_question
call GET "/tasks" "$operator"
expect 200
json 'len([t for t in b["data"] if t["product_id"] == "'"$product"'"]) == 1' >/dev/null
call GET "/products/$product" "$operator"
expect 200
[ "$(json '[(f["key"], str(f["value"])) for f in b["photo_facts"]]')" = "$facts_before" ] \
  || fail "the second marketplace changed the photo values"

step answer_maroon_200
call POST "/tasks/$task/answer" "$operator" "e2e-si3-answer-$run" '{"value": "maroon"}'
expect 200
json 'b["status"] == "applied"' >/dev/null

step answer_replay_200
call POST "/tasks/$task/answer" "$operator" "e2e-si3-answer-$run" '{"value": "maroon"}'
expect 200

step colour_is_the_sellers
call GET "/products/$product" "$operator"
expect 200
json 'b["attributes"]["color"] == "maroon"' >/dev/null
json '"color" not in [f["key"] for f in b["photo_facts"]]' >/dev/null

echo "e2e: photo facts OK"
