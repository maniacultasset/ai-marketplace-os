#!/usr/bin/env bash
# The S20 marketplace preview e2e, HTTP only apart from one read-only count (S20 T21): the
# migrator's `errors load` loads the fake error map twice without adding a row; an acme admin
# previews KURTA-001 (standalone, pinned to the unmapped v1 kurta template: 200, one item, no
# product type, CATEGORY_NOT_MAPPED) and the demo kurta family (pinned to the Amazon kurta
# mapping: the parent then 4 children, product type KURTA, no errors); no token is 401; a bharat
# admin gets 404 for acme's listing. Two logins per run. Read-only: the preview sends nothing.
#
# Usage: ERRORS_LOAD=<cmd> scripts/e2e/marketplace_preview.sh [base_url]
# ERRORS_LOAD is a shell command that gets the error-map file as "$1" (the Makefile passes the
# migrator's `python -m app.cli errors load --file`). The only non-HTTP step besides the hook is
# `db_error_map_count_same`: a read-only `SELECT count(*)` through the compose postgres.
# Local only (the demo seed's public password): any other host exits 2 before a request.
# Needs the compose stack with the demo seed (`make dev` + `make seed`).
# On failure it prints the step name and the HTTP status seen, never a response body.
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  echo "usage: ERRORS_LOAD=<cmd> $0 [base_url]" >&2
  exit 2
}
[ -n "${ERRORS_LOAD:-}" ] || usage

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
api="$base/api/v1"
password="devpassword123" # the demo seed's local-only password (A§5)
error_map="apps/api/tests/fixtures/error_map.fake.json"
seed_namespace="5eed0000-9c1e-4d6a-8a51-0c5a1e5eed00" # app.db.seed_ops.SEED_NAMESPACE

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

# call METHOD PATH [TOKEN]: the status goes in $status, the body in $body.
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
    scope = {"__builtins__": {}, "b": b, "len": len, "str": str, "any": any, "all": all}
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

# seed_id NAME: the demo seed's fixed uuid5 id for NAME.
seed_id() { python3 -c 'import sys, uuid; print(uuid.uuid5(uuid.UUID(sys.argv[1]), sys.argv[2]))' "$seed_namespace" "$1"; }
demo_listing="$(seed_id acme:demo-listing)"
family_listing="$(seed_id acme:demo-family:listing)"

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step errors_load
bash -c "$ERRORS_LOAD" errors_load "$error_map" >/dev/null || fail "the ERRORS_LOAD hook failed"

step errors_load_again
bash -c "$ERRORS_LOAD" errors_load "$error_map" >/dev/null || fail "the ERRORS_LOAD hook failed"

step db_error_map_count_same
# the one read-only database read: a count, before and after one more load
count_sql='SELECT count(*) FROM marketplace_error_map'
before="$(docker compose exec -T postgres psql -U postgres -d marketplace_os -tAX -c "$count_sql")" || fail "the error-map count failed"
bash -c "$ERRORS_LOAD" errors_load "$error_map" >/dev/null || fail "the ERRORS_LOAD hook failed"
after="$(docker compose exec -T postgres psql -U postgres -d marketplace_os -tAX -c "$count_sql")" || fail "the error-map count failed"
[ "$before" = "$after" ] && [ "$after" -gt 0 ] || fail "errors load changed the row count"

step login_acme_admin
admin="$(login admin@acme.test)"

step preview_demo_200
call GET "/listings/$demo_listing/preview" "$admin"
expect 200
json 'len(b["items"]) == 1 and b["items"][0]["item_role"] == "standalone" and b["product_type"] is None and [e["error_code"] for e in b["report"]["errors"]] == ["CATEGORY_NOT_MAPPED"]' >/dev/null

step preview_family_200
call GET "/listings/$family_listing/preview" "$admin"
expect 200
json 'b["product_type"] == "KURTA" and [i["item_role"] for i in b["items"]] == ["parent", "child", "child", "child", "child"] and b["report"]["errors"] == []' >/dev/null

step no_token_401
call GET "/listings/$family_listing/preview"
expect 401

step login_bharat_admin
bharat="$(login admin@bharat.test)"

step bharat_preview_404
call GET "/listings/$family_listing/preview" "$bharat"
expect 404
json 'b["error"]["type"] == "NOT_FOUND"' >/dev/null

echo "e2e: marketplace preview OK"
