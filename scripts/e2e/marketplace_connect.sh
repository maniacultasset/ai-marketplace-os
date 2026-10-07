#!/usr/bin/env bash
# The S15 marketplace connect flow, HTTP only, against the local fake marketplace: an acme owner
# connects Amazon (connect -> consent on the fake -> callback -> confirm), a replayed callback and
# a bharat confirm of acme's handle are refused, a reviewer may not connect, the stored token is
# ciphertext; a rejected refresh puts the account in needs_reconnect with one reconnect task; the
# same seller reconnects (same account, task applied); revoke deletes the tokens, twice.
#
# Usage: scripts/e2e/marketplace_connect.sh [base_url]   default http://127.0.0.1:<API_HOST_PORT>
# Hooks (from `make e2e`): FAKE_URL (the fake marketplace, default
# http://127.0.0.1:<FAKE_MARKETPLACE_HOST_PORT>) and REFRESH_ONCE (a command that runs one refresh
# pass). The API must run with its marketplace URLs on the fake (`make dev` exports them).
# Local only: any other host exits 2 before a request; no hook exits 2. Every wait is bounded at
# 60 s. On failure it prints the step name and the HTTP status seen, never a response body.
# Post-condition: no live acme Amazon account is left (the last account step revokes it).
set -euo pipefail
cd "$(dirname "$0")/../.."

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
fake="${FAKE_URL:-http://127.0.0.1:${FAKE_MARKETPLACE_HOST_PORT:-4020}}"
python3 scripts/ci/local_url.py "$fake" || exit 2
fake="${fake%/}"
if [ -z "${REFRESH_ONCE:-}" ]; then
  echo "usage: FAKE_URL=<fake> REFRESH_ONCE=<one refresh pass> $0 [base_url] (make e2e sets both)" >&2
  exit 2
fi
api="$base/api/v1"
password="devpassword123" # the demo seed's local-only password (A§5)
seller="E2E$(date +%s)$RANDOM"
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
    scope = {"__builtins__": {}, "b": b, "len": len, "str": str, "any": any}
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

sql() {
  docker compose exec -T postgres psql -U postgres -d marketplace_os -tA -c "$1" \
    || fail "the database query failed"
}

login() {
  call POST "$api/auth/login" "" "{\"email\": \"$1\", \"password\": \"$password\"}"
  expect 200
  json 'b["access_token"]'
}

# connect_confirm TOKEN: connect -> consent (as $seller) -> callback -> confirm; the account id.
connect_confirm() {
  local token="$1" url callback handle
  call POST "$api/marketplaces/connect" "$token" '{"marketplace": "amazon"}'
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
  call POST "$api/marketplaces/connect/confirm" "$token" "{\"pending\": \"$handle\"}"
  expect 200
  json 'b["id"]'
}

# run_bounded CMD: run the hook command, killed after $bound seconds.
run_bounded() {
  bash -c "$1" &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$bound" ]; then
      kill "$pid" 2>/dev/null || true
      fail "the refresh pass ran over ${bound}s"
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid" || fail "the refresh pass failed"
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$bound" "$base/readyz")" || status="000"
expect 200

step fake_health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$bound" "$fake/healthz")" || status="000"
expect 200
# tokens that expire within the refresh window, so the refresh pass below picks this account
call POST "$fake/__control" "" '{"expires_in": 600}'
expect 200

step login_acme_owner
owner="$(login owner@acme.test)"

step cleanup_live_amazon
call GET "$api/marketplaces/accounts?limit=200" "$owner"
expect 200
for stale in $(json '" ".join(a["id"] for a in b["data"] if a["marketplace"] == "amazon" and a["status"] != "revoked")'); do
  call POST "$api/marketplaces/accounts/$stale/revoke" "$owner"
  expect 200
done

step connect_200
call POST "$api/marketplaces/connect" "$owner" '{"marketplace": "amazon"}'
expect 200
authorize_url="$(json 'b["authorize_url"]')"
case "$authorize_url" in "$fake"/*) ;; *) fail "the consent URL is not the fake's" ;; esac

step consent
call GET "$authorize_url&seller=$seller"
expect 302
callback="$base$(path_query "$location")"
[ -n "$(query "$callback" spapi_oauth_code)" ] || fail "the fake sent no code"

step callback_303
call GET "$callback"
expect 303
handle="$(query "$location" pending)"
[ -n "$handle" ] || fail "no pending handle"

step callback_replay_409
call GET "$callback"
expect 409
json 'b["error"]["type"] == "OAUTH_STATE_INVALID"' >/dev/null

step login_bharat_owner
bharat="$(login owner@bharat.test)"

step bharat_confirm_409
call POST "$api/marketplaces/connect/confirm" "$bharat" "{\"pending\": \"$handle\"}"
expect 409
json 'b["error"]["type"] == "OAUTH_STATE_INVALID"' >/dev/null

step confirm_200
call POST "$api/marketplaces/connect/confirm" "$owner" "{\"pending\": \"$handle\"}"
expect 200
account_id="$(json 'b["id"]')"
json 'b["status"] == "active" and b["seller_id"] == "'"$seller"'"' >/dev/null

step list_active
call GET "$api/marketplaces/accounts?limit=200" "$owner"
expect 200
json 'any(a["id"] == "'"$account_id"'" and a["status"] == "active" for a in b["data"])' >/dev/null

step login_acme_reviewer
reviewer="$(login reviewer@acme.test)"

step reviewer_connect_403
call POST "$api/marketplaces/connect" "$reviewer" '{"marketplace": "amazon"}'
expect 403

step db_ciphertext_no_canary
sealed="$(sql "SELECT count(*) FROM marketplace_accounts WHERE id = '$account_id' AND token_enc IS NOT NULL AND kms_key_version = 'local-1' AND position(convert_to('CANARYzq9', 'UTF8') in token_enc) = 0")"
[ "$sealed" = "1" ] || fail "the stored token is not sealed ciphertext"

step force_invalid_grant
call POST "$fake/__control" "" '{"next_refresh": "invalid_grant"}'
expect 200

step refresh_once
run_bounded "$REFRESH_ONCE"

step list_needs_reconnect
call GET "$api/marketplaces/accounts?limit=200" "$owner"
expect 200
json 'any(a["id"] == "'"$account_id"'" and a["status"] == "needs_reconnect" for a in b["data"])' >/dev/null

step db_one_reconnect_task
tasks="$(sql "SELECT count(*) FROM human_tasks WHERE marketplace_account_id = '$account_id' AND kind = 'reconnect_account' AND status = 'open'")"
[ "$tasks" = "1" ] || fail "want one open reconnect task"

step reconnect
again="$(connect_confirm "$owner")"
[ "$again" = "$account_id" ] || fail "the reconnect made a new account"
call GET "$api/marketplaces/accounts?limit=200" "$owner"
expect 200
json 'any(a["id"] == "'"$account_id"'" and a["status"] == "active" for a in b["data"])' >/dev/null
applied="$(sql "SELECT count(*) FROM human_tasks WHERE marketplace_account_id = '$account_id' AND status = 'applied'")"
[ "$applied" = "1" ] || fail "the reconnect task is not applied"

step revoke_200
call POST "$api/marketplaces/accounts/$account_id/revoke" "$owner"
expect 200
json 'b["status"] == "revoked"' >/dev/null

step revoke_again_200
call POST "$api/marketplaces/accounts/$account_id/revoke" "$owner"
expect 200
json 'b["status"] == "revoked"' >/dev/null

step db_revoked_shredded
shredded="$(sql "SELECT count(*) FROM marketplace_accounts WHERE id = '$account_id' AND status = 'revoked' AND token_enc IS NULL AND kms_key_version IS NULL")"
[ "$shredded" = "1" ] || fail "the revoked account still holds its tokens"

echo "e2e: marketplace_connect OK ($seller)"
