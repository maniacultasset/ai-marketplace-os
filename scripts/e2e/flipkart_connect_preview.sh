#!/usr/bin/env bash
# The S22 Flipkart connect and preview e2e, HTTP only, against the local fake marketplace (S22
# T18; AC16): a second API with FLIPKART_ENABLED=false refuses Flipkart connect with the 501
# ErrorReport and sends nothing to the fake; on the flag-on API (`make dev` turns the flag on
# locally) an acme owner connects Flipkart (connect -> consent on the fake -> callback ->
# confirm), previews the demo Flipkart kurta family (4 children, no parent row, the Flipkart
# vertical), bharat gets 404 for acme's listing, no token is 401, and the account is revoked.
#
# Usage: FAKE_URL=<fake> FLAG_OFF_URL=<flag-off API> scripts/e2e/flipkart_connect_preview.sh [base_url]
# `make e2e` sets both hooks: it runs the fake for the whole pass and starts the flag-off API on
# FLAG_OFF_API_HOST_PORT. Local only: any other host exits 2 before a request; no hook exits 2.
# Every wait is bounded at 60 s. On failure it prints the step name and the HTTP status seen,
# never a response body. Needs the demo seed (`make seed`).
# Post-condition: no live acme Flipkart account is left (the last step revokes it).
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  echo "usage: FAKE_URL=<fake> FLAG_OFF_URL=<flag-off API> $0 [base_url] (make e2e sets both)" >&2
  exit 2
}

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
[ -n "${FAKE_URL:-}" ] && [ -n "${FLAG_OFF_URL:-}" ] || usage
fake="$FAKE_URL"
python3 scripts/ci/local_url.py "$fake" || exit 2
fake="${fake%/}"
off="$FLAG_OFF_URL"
python3 scripts/ci/local_url.py "$off" || exit 2
off="${off%/}"
api="$base/api/v1"
off_api="$off/api/v1"
password="devpassword123" # the demo seed's local-only password (A§5)
seller="E2EFK$(date +%s)$RANDOM"
seed_namespace="5eed0000-9c1e-4d6a-8a51-0c5a1e5eed00" # app.db.seed_ops.SEED_NAMESPACE
bound=60

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
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

# call METHOD URL [TOKEN] [JSON]: the status goes in $status, the body in $body, any Location
# in $location. Redirects are never followed.
call() {
  local method="$1" url="$2" token="${3:-}" data="${4:-}" out
  local args=(-s -o "$body" -w '%{http_code} %{redirect_url}' --max-time "$bound" -X "$method"
    -H 'Accept: application/json')
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$data" ]; then args+=(-H 'Content-Type: application/json' --data-binary "$data"); fi
  out="$(curl "${args[@]}" "$url")" || out="000 "
  status="${out%% *}"
  location="${out#* }"
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

# query URL NAME: one query parameter of URL (empty when absent).
query() {
  python3 - "$1" "$2" <<'PY'
import sys
from urllib.parse import parse_qs, urlsplit
values = parse_qs(urlsplit(sys.argv[1]).query).get(sys.argv[2], [""])
print(values[0])
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
  call POST "$api/auth/login" "" "{\"email\": \"$1\", \"password\": \"$password\"}"
  expect 200
  json 'b["access_token"]'
}

# seed_id NAME: the demo seed's fixed uuid5 id for NAME.
seed_id() { python3 -c 'import sys, uuid; print(uuid.uuid5(uuid.UUID(sys.argv[1]), sys.argv[2]))' "$seed_namespace" "$1"; }
family_listing="$(seed_id acme:demo-flipkart:KURTA-FAM-01)"

step health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$bound" "$base/readyz")" || status="000"
expect 200

step fake_health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$bound" "$fake/healthz")" || status="000"
expect 200

step off_health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$bound" "$off/readyz")" || status="000"
expect 200

step login_acme_owner
owner="$(login owner@acme.test)"

step fake_reset_calls
call POST "$fake/__control" "" '{"reset_calls": true}'
expect 200

step off_connect_501
call POST "$off_api/marketplaces/connect" "$owner" '{"marketplace": "flipkart"}'
expect 501
json 'b["error"]["type"] == "NOT_IMPLEMENTED" and b["error"]["error_report"]["error_code"] == "NOT_IMPLEMENTED"' >/dev/null
json 'b["error"]["message"] == "Flipkart is not enabled"' >/dev/null

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

step connect_200
call POST "$api/marketplaces/connect" "$owner" '{"marketplace": "flipkart"}'
expect 200
authorize_url="$(json 'b["authorize_url"]')"
case "$authorize_url" in "$fake"/*) ;; *) fail "the consent URL is not the fake's" ;; esac
[ "$(query "$authorize_url" code_challenge_method)" = "S256" ] || fail "no S256 PKCE challenge"

step consent
call GET "$authorize_url&seller=$seller"
expect 302
callback="$base$(path_query "$location")"
[ -n "$(query "$callback" code)" ] || fail "the fake sent no code"

step callback_303
call GET "$callback"
expect 303
handle="$(query "$location" pending)"
[ -n "$handle" ] || fail "no pending handle"

step confirm_200
call POST "$api/marketplaces/connect/confirm" "$owner" "{\"pending\": \"$handle\"}"
expect 200
account_id="$(json 'b["id"]')"
json 'b["marketplace"] == "flipkart" and b["status"] == "active" and b["seller_id"] == "'"$seller"'"' >/dev/null

step list_active
call GET "$api/marketplaces/accounts?limit=200" "$owner"
expect 200
json 'any(a["id"] == "'"$account_id"'" and a["status"] == "active" for a in b["data"])' >/dev/null

step preview_family_200
call GET "$api/listings/$family_listing/preview" "$owner"
expect 200
json 'b["marketplace"] == "flipkart" and b["product_type"] == "kurta"' >/dev/null
json 'len(b["items"]) == 4 and all(i["item_role"] == "child" for i in b["items"])' >/dev/null
json 'all(i["seller_sku"] != "KURTA-FAM-01" for i in b["items"])' >/dev/null
json 'all(i["payload"]["attributes"]["style_code"] == [{"value": "KURTA-FAM-01"}] for i in b["items"])' >/dev/null
json 'b["report"]["errors"] == []' >/dev/null

step login_bharat_owner
bharat="$(login owner@bharat.test)"

step bharat_preview_404
call GET "$api/listings/$family_listing/preview" "$bharat"
expect 404

step no_token_401
call GET "$api/listings/$family_listing/preview"
expect 401

step revoke_200
call POST "$api/marketplaces/accounts/$account_id/revoke" "$owner"
expect 200
json 'b["status"] == "revoked"' >/dev/null

echo "e2e: flipkart_connect_preview OK ($seller)"
