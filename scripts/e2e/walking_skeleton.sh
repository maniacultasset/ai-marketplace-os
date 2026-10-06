#!/usr/bin/env bash
# The S12 walking skeleton, HTTP only (CODE-PLAN S12 T14; PLAN D8): an acme operator logs in and
# creates, reads and lists a product; a duplicate SKU is 409; a reviewer may not create; no token
# is 401; a bharat operator gets 404 for the id and never sees it in its list; the row is stored
# under acme. Rerunnable: every run uses a fresh SKU.
#
# Usage: scripts/e2e/walking_skeleton.sh [base_url]   default http://127.0.0.1:<API_HOST_PORT>
# Local only (the demo seed's public password): any other host exits 2 before a request.
# Needs the compose stack with the demo seed (`make dev` + `make seed`, or bootstrap's image).
# On failure it prints the step name and the HTTP status seen, never a response body.
set -euo pipefail
cd "$(dirname "$0")/../.."

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
api="$base/api/v1"
password="devpassword123" # the demo seed's local-only password (A§5)
sku="E2E-$(date +%s)-$RANDOM"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
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

# call METHOD PATH [TOKEN] [JSON]: the status goes in $status, the body in $body.
call() {
  local method="$1" path="$2" token="${3:-}" data="${4:-}"
  local args=(-s -o "$body" -w '%{http_code}' -X "$method" -H 'Accept: application/json')
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$data" ]; then args+=(-H 'Content-Type: application/json' --data-binary "$data"); fi
  status="$(curl "${args[@]}" "$api$path")" || status="000"
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
    scope = {"__builtins__": {}, "b": b, "len": len, "str": str}
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
  call POST /auth/login "" "{\"email\": \"$1\", \"password\": \"$password\"}"
  expect 200
  json 'b["access_token"]'
}

# every_page TOKEN: the ids of every product on every page, one per line.
every_page() {
  local token="$1" cursor="" ids="" next
  while :; do
    if [ -n "$cursor" ]; then call GET "/products?limit=200&cursor=$cursor" "$token"; else call GET "/products?limit=200" "$token"; fi
    expect 200
    ids="$ids$(json '"\n".join(str(p["id"]) for p in b["data"])')"$'\n'
    next="$(json 'b["page"]["next_cursor"]')"
    if [ -z "$next" ]; then break; fi
    cursor="$next"
  done
  printf '%s' "$ids"
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step login_acme_operator
operator="$(login operator@acme.test)"

step create_201
call POST /products "$operator" "{\"sku\": \"$sku\", \"title\": \"E2E Organic Cotton Kurta\", \"category\": \"kurtas\", \"attributes\": {\"color\": \"navy blue\", \"material\": \"organic cotton\"}}"
expect 201
json 'b["sku"] == "'"$sku"'" and b["status"] == "draft" and b["attributes"] == {"color": "navy blue", "fabric_type": "organic cotton"}' >/dev/null
product_id="$(json 'b["id"]')"
python3 -c 'import sys, uuid; uuid.UUID(sys.argv[1])' "$product_id" || fail "the id is not a UUID"

step get_200
call GET "/products/$product_id" "$operator"
expect 200
json 'b["id"] == "'"$product_id"'" and b["sku"] == "'"$sku"'"' >/dev/null

step list_contains
acme_ids="$(every_page "$operator")"
grep -qx "$product_id" <<<"$acme_ids" || fail "the new product is not in acme's list"

step create_again_409
call POST /products "$operator" "{\"sku\": \"$sku\", \"title\": \"Again\", \"category\": \"kurtas\"}"
expect 409
json 'b["error"]["type"] == "SKU_CONFLICT"' >/dev/null

step login_acme_reviewer
reviewer="$(login reviewer@acme.test)"

step reviewer_create_403
call POST /products "$reviewer" "{\"sku\": \"$sku-R\", \"title\": \"Reviewer\", \"category\": \"kurtas\"}"
expect 403
json 'b["error"]["type"] == "PERMISSION_DENIED"' >/dev/null

step no_token_401
call GET "/products/$product_id"
expect 401

step login_bharat_operator
bharat="$(login operator@bharat.test)"

step bharat_get_404
call GET "/products/$product_id" "$bharat"
expect 404
json 'b["error"]["type"] == "NOT_FOUND"' >/dev/null

step bharat_list_lacks
bharat_ids="$(every_page "$bharat")"
if grep -qx "$product_id" <<<"$bharat_ids"; then fail "bharat's list shows acme's product"; fi

step db_row_under_acme
under_acme="$(docker compose exec -T postgres psql -U postgres -d marketplace_os -tA -c \
  "SELECT p.tenant_id = t.id FROM products p JOIN tenants t ON t.slug = 'acme' WHERE p.id = '$product_id'")" \
  || fail "the database query failed"
[ "$under_acme" = "t" ] || fail "the row is not stored under acme"

echo "e2e: walking_skeleton OK ($sku)"
