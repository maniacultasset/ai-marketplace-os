#!/usr/bin/env bash
# The S17b image pipeline, HTTP only (CODE-PLAN S17b T18; AC14): an acme operator creates a
# product, mints a signed upload URL, PUTs a generated 800x800 white JPEG to the object store with
# the returned headers (a second PUT of the URL is 412, another length 403), registers the key,
# and the worker's `image_compliance` job writes the reports (Amazon RESOLUTION_LOW and failed,
# Flipkart passed with a warning). A replay of the key returns the same image, the key with
# another body is 409. A multipart 1000x1000 PNG main then replaces the first main (its report is
# 404) and the product lists its live key. A reviewer may not mint a URL (403), no token is 401,
# and bharat gets 404 on acme's report and on a key under acme's prefix.
#
# Usage: WORKER_START=<cmd> WORKER_STOP=<cmd> scripts/e2e/image_pipeline.sh [base_url]
# E2E_SIGNED_LEG=0 skips the signed-PUT leg (the bootstrap image signs for the compose network's
# `minio` host, which this script cannot reach); a multipart main stands in for the replaced one. The approve leg
# (AC17) waits on S19's approve endpoint and is skipped by name. Local only: the base URL and
# every signed URL must be on 127.0.0.1 or localhost, else exit 2 before the request. Every poll
# gives up after 60 s. On failure it prints the step name and the HTTP status, never a body.
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
image 1000 1000 255,255,255 PNG "$work/white_1000.png"
image 640 640 255,255,255 JPEG "$work/small.jpg"
size_800="$(wc -c <"$work/white_800.jpg" | tr -d ' ')"

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step login_acme_operator
operator="$(login operator@acme.test)"

step create_product_201
sku="E2E-IMG-$run"
call POST /products "$operator" "" "{\"sku\": \"$sku\", \"title\": \"E2E Image Kurta\", \"category\": \"kurtas\"}"
expect 201
product="$(json 'b["id"]')"

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

if [ "$signed_leg" = "1" ]; then
  step upload_url_200
  call POST /images/upload-url "$operator" "" "{\"product_id\": \"$product\", \"slot\": \"main\", \"content_type\": \"image/jpeg\", \"bytes\": $size_800}"
  expect 200
  url="$(json 'b["upload_url"]')"
  storage_key="$(json 'b["storage_key"]')"
  json 'b["headers"] == {"Content-Type": "image/jpeg", "If-None-Match": "*"}' >/dev/null

  step put_signed_200
  put_signed "$url" image/jpeg "$work/white_800.jpg"
  expect 200

  step put_signed_again_412
  put_signed "$url" image/jpeg "$work/white_800.jpg"
  expect 412

  step put_wrong_length_403
  call POST /images/upload-url "$operator" "" "{\"product_id\": \"$product\", \"slot\": \"main\", \"content_type\": \"image/jpeg\", \"bytes\": $size_800}"
  expect 200
  other_url="$(json 'b["upload_url"]')"
  put_signed "$other_url" image/jpeg "$work/small.jpg"
  expect 403

  step register_202
  key="e2e-img-$run"
  upload "$operator" "$key" "$product" main "storage_key=$storage_key"
  expect 202
  image_id="$(json 'b["image_id"]')"
  job_id="$(json 'b["job_id"]')"

  step job_succeeded
  wait_job "$operator" "$job_id" succeeded

  step report_amazon_resolution_low
  call GET "/images/$image_id/report" "$operator"
  expect 200
  json 'b["compliance_status"] == "failed" and b["reports"]["amazon"]["pass"] is False and [i["code"] for i in b["reports"]["amazon"]["issues"]] == ["RESOLUTION_LOW"]' >/dev/null

  step report_flipkart_pass_with_warning
  json 'b["reports"]["flipkart"]["pass"] is True and [i["code"] for i in b["reports"]["flipkart"]["issues"]] == ["RESOLUTION_LOW"]' >/dev/null

  step replay_same_key_same_image
  upload "$operator" "$key" "$product" main "storage_key=$storage_key"
  expect 202
  json 'b["image_id"] == "'"$image_id"'" and b["job_id"] == "'"$job_id"'"' >/dev/null

  step idem_key_other_body_409
  upload "$operator" "$key" "$product" secondary "storage_key=$storage_key"
  expect 409
  json 'b["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"' >/dev/null
else
  echo "e2e: signed leg skipped (E2E_SIGNED_LEG=0)"
  step multipart_seed_main_202
  upload "$operator" "e2e-img-seed-$run" "$product" main "file=$work/white_800.jpg"
  expect 202
  image_id="$(json 'b["image_id"]')"
  wait_job "$operator" "$(json 'b["job_id"]')" succeeded
fi

step multipart_main_1000_white_png_202
upload "$operator" "e2e-img-png-$run" "$product" main "file=$work/white_1000.png"
expect 202
new_image="$(json 'b["image_id"]')"
new_job="$(json 'b["job_id"]')"

step job_succeeded_2
wait_job "$operator" "$new_job" succeeded
call GET "/images/$new_image/report" "$operator"
expect 200
json 'b["compliance_status"] == "passed"' >/dev/null

step old_main_report_404
call GET "/images/$image_id/report" "$operator"
expect 404

step product_lists_live_keys
call GET "/products/$product" "$operator"
expect 200
json 'len(b["images"]) == 1 and b["images"][0].endswith("/images/'"$new_image"'")' >/dev/null

step login_acme_reviewer
reviewer="$(login reviewer@acme.test)"

step reviewer_upload_url_403
call POST /images/upload-url "$reviewer" "" "{\"product_id\": \"$product\", \"slot\": \"main\", \"content_type\": \"image/jpeg\", \"bytes\": 10}"
expect 403

step no_token_401
call GET "/images/$new_image/report"
expect 401

step login_bharat_operator
bharat="$(login operator@bharat.test)"

step bharat_report_404
call GET "/images/$new_image/report" "$bharat"
expect 404

step bharat_upload_foreign_key_404
call POST /products "$bharat" "" "{\"sku\": \"$sku-B\", \"title\": \"Bharat Kurta\", \"category\": \"kurtas\"}"
expect 201
bharat_product="$(json 'b["id"]')"
call GET "/products/$product" "$operator"
acme_key="$(json 'b["images"][0]')"
foreign="${acme_key%%/*}/$bharat_product/images/$(python3 -c 'import uuid; print(uuid.uuid4())')"
upload "$bharat" "e2e-img-bharat-$run" "$bharat_product" main "storage_key=$foreign"
expect 404

echo "e2e: approve leg skipped (S19's approve endpoint is not built; AC17 moves to S19)"

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

echo "e2e: image_pipeline OK (image $new_image)"
