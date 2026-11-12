#!/usr/bin/env bash
# SI1 seller intake, HTTP only (CODE-PLAN SI1 T24; AC1, AC4, AC6, AC8, AC12, AC15): an acme owner
# sets the brand default manufacturer (PATCH /brand-profile under If-Match); an operator mints an
# upload URL with no product (a fresh product id), PUTs a generated JPEG, creates the product with
# every intake field and `image_uploads` under that id, reads it back with its ETag, edits it
# under If-Match (a stale tag is 409, the same body under the stale tag is 200), sees the
# manufacturer inherited after `manufacturer: null`, and deletes the image (204, then 404). An
# anonymous listing of the public image bucket is refused (403): the bucket serves objects by
# name only (ADR-0026, the compose `minio-init-public` GetObject-only policy).
#
# The generate -> approve -> publish leg that hands the fake marketplace a public URL is proved by
# `tests/integration/test_publish_public_images.py` (the stub pipeline makes no image version on
# this path); it is skipped here by name, as image_pipeline.sh skips its approve leg.
#
# Usage: WORKER_START=<cmd> WORKER_STOP=<cmd> scripts/e2e/seller_intake.sh [base_url]
# E2E_SIGNED_LEG=0 skips the minted-id signed PUT (the create then runs without images).
# IMAGE_PUBLIC_BASE_URL names the local public bucket (default the compose MinIO's). Local only.
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
signed_leg="${E2E_SIGNED_LEG:-1}"
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
image 800 800 255,255,255 JPEG "$work/white_800.jpg"
size_800="$(wc -c <"$work/white_800.jpg" | tr -d ' ')"
# The stack's own MinIO (this checkout's or a fresh stack's port); local_url.py takes an origin
# only, so the bucket path is checked apart.
public_base="${IMAGE_PUBLIC_BASE_URL:-http://localhost:${MINIO_API_HOST_PORT:-9000}/marketplace-images-public}"
python3 scripts/ci/local_url.py "${public_base%/marketplace-images-public}" || exit 2
case "$public_base" in */marketplace-images-public) ;; *) echo "refused: IMAGE_PUBLIC_BASE_URL must end in /marketplace-images-public" >&2; exit 2 ;; esac
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

party='{"legal_name": "E2E Default Maker Pvt Ltd", "address_line1": "1 Residency Road", "city": "Bengaluru", "state_code": "KA", "postal_code": "560025"}'

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step login_acme_owner
owner="$(login owner@acme.test)"
step login_acme_operator
operator="$(login operator@acme.test)"

step brand_profile_get_etag
brand_tag="$(etag_of GET /brand-profile "$owner")"
[ -n "$brand_tag" ] || fail "no ETag on the brand profile"

step brand_profile_patch_200
call_if PATCH /brand-profile "$owner" "$brand_tag" "{\"default_manufacturer\": $party}"
expect 200
json 'b["default_manufacturer"]["legal_name"] == "E2E Default Maker Pvt Ltd"' >/dev/null

step brand_profile_operator_403
call_if PATCH /brand-profile "$operator" "$(etag_now)" '{"default_manufacturer": null}'
expect 403

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

sku="E2E-SI1-$run"
uploads=""
id_field=""
if [ "$signed_leg" = "1" ]; then
  step upload_url_minted_200
  call POST /images/upload-url "$operator" "" "{\"slot\": \"main\", \"content_type\": \"image/jpeg\", \"bytes\": $size_800}"
  expect 200
  url="$(json 'b["upload_url"]')"
  storage_key="$(json 'b["storage_key"]')"
  minted="$(json 'b["product_id"]')"

  step put_signed_200
  put_signed "$url" image/jpeg "$work/white_800.jpg"
  expect 200
  uploads=", \"image_uploads\": [{\"storage_key\": \"$storage_key\", \"slot\": \"main\"}]"
  id_field=", \"id\": \"$minted\""
fi

step create_with_every_field_201
call POST /products "$operator" "" "{\"sku\": \"$sku\", \"title\": \"E2E Intake Kurta\", \"category\": \"kurtas\", \"style_number\": \"ST-$run\", \"mrp\": \"1499.00\", \"selling_price\": \"1299\", \"size_chart\": {\"unit\": \"cm\", \"basis\": \"garment\", \"rows\": [{\"size\": \"M\", \"chest\": 101}]}, \"attributes\": {\"fabric_composition\": \"60% Cotton, 40% Polyester\"}$id_field$uploads}"
expect 201
product="$(json 'b["id"]')"
json 'b["mrp"] == "1499.00" and b["selling_price"] == "1299.00"' >/dev/null
json 'b["fiber_composition"] == [{"fibre": "cotton", "percent": 60}, {"fibre": "polyester", "percent": 40}]' >/dev/null
json 'b["manufacturer"]["inherited"] == True' >/dev/null

step get_with_etag
tag="$(etag_of GET "/products/$product" "$operator")"
[ -n "$tag" ] || fail "no ETag on the product"

step patch_200
call_if PATCH "/products/$product" "$operator" "$tag" '{"title": "E2E Intake Kurta v2", "manufacturer": {"legal_name": "E2E Own Maker Pvt Ltd", "address_line1": "2 MG Road", "city": "Bengaluru", "state_code": "KA", "postal_code": "560001"}}'
expect 200
json 'b["manufacturer"]["inherited"] == False' >/dev/null
new_tag="$(etag_now)"

step patch_stale_409
call_if PATCH "/products/$product" "$operator" "$tag" '{"title": "E2E Intake Kurta v3"}'
expect 409

step patch_same_body_stale_200
call_if PATCH "/products/$product" "$operator" "$tag" '{"title": "E2E Intake Kurta v2"}'
expect 200
[ "$(etag_now)" = "$new_tag" ] || fail "a no-op edit moved the ETag"

step patch_manufacturer_null_inherits
call_if PATCH "/products/$product" "$operator" "$new_tag" '{"manufacturer": null}'
expect 200
json 'b["manufacturer"]["inherited"] == True' >/dev/null

if [ "$signed_leg" = "1" ]; then
  step image_listed
  call GET "/products/$product" "$operator"
  expect 200
  image_id="$(json 'b["image_details"][0]["image_id"]')"

  step delete_image_204
  call DELETE "/images/$image_id" "$operator"
  expect 204

  step delete_image_again_404
  call DELETE "/images/$image_id" "$operator"
  expect 404
fi

step public_bucket_not_listable_403
status="$(curl -s -o /dev/null -w '%{http_code}' "$public_base/")" || status="000"
expect 403

echo "e2e: seller intake OK"
