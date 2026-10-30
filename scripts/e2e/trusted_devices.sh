#!/usr/bin/env bash
# Trusted browsers (ADR-0023), HTTP only after the staff CLI setup. Local runs use
# MFA_ENFORCEMENT=off, where a browser is never trusted: this script proves the contract holds
# there (the new fields are accepted and ignored, the device routes answer, sign out all keeps the
# caller's session and ends the others). The legs that need a TOTP code (trust, skip the code,
# rotation, reuse, removal, the 30-minute step-up) are covered by the integration suite
# (apps/api/tests/integration/test_auth_trusted_devices.py).
#
#   setup_tenant             the staff CLI: one run-unique tenant and owner
#   login_with_token_off     a login that sends a device token answers signed_in, no device fields
#   list_empty_200           GET /auth/devices: no trusted browser
#   remove_unknown_404       DELETE /auth/devices/{unknown}: 404 NOT_FOUND
#   second_session_200       a second login (another session of the same user)
#   sign_out_all_204         POST /auth/devices/sign-out-all from the second session
#   other_session_ended_401  the first session's refresh is 401
#   this_session_kept_200    the second session's refresh still rotates
#
# Usage: MKOS_CLI=<cmd prefix> scripts/e2e/trusted_devices.sh [base_url]
# Local only: any other host exits 2 before a request. Rerunnable: run-unique emails and slugs.
# At most 2 logins for the one email; no poll. On failure it names the step and the HTTP status
# seen, never a body.
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  echo "usage: MKOS_CLI=<cmd prefix> $0 [base_url]" >&2
  exit 2
}
[ -n "${MKOS_CLI:-}" ] || usage

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
api="$base/api/v1"
run="$(date +%s)$RANDOM"
slug="e2e-trust-$run"
user_email="trust-$run@example.test"
staff_email="staff@example.test"

work="$(mktemp -d)"
chmod 0700 "$work"
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

expect() {
  if [ "$status" != "$1" ]; then fail "want HTTP $1"; fi
}

# call METHOD PATH [TOKEN] [KEY] [JSON_FILE]: the status goes in $status, the body in $body. A
# JSON body always comes from a file, so no password or token is ever on an argv.
call() {
  local method="$1" path="$2" token="${3:-}" key="${4:-}" data="${5:-}"
  local args=(-s -o "$body" -w '%{http_code}' --max-time 60 -X "$method" -H 'Accept: application/json')
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$key" ]; then args+=(-H "Idempotency-Key: $key"); fi
  if [ -n "$data" ]; then args+=(-H 'Content-Type: application/json' --data-binary "@$data"); fi
  status="$(curl "${args[@]}" "$api$path")" || status="000"
}

# json EXPR: evaluate a Python expression over the parsed body `b`; prints the result; a false
# or failing expression fails the step.
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

# A random 128-bit number plus a fixed "-Kq": always meets the DPP 1.4.1 rules (12+ characters,
# upper, lower, digit, symbol) and, with no 3-letter run, can never hold a part of the user's name.
new_password() {
  python3 -c 'import secrets; print("%039d-Kq" % secrets.randbits(128))' >"$1"
}

# cli PASSWORD_FILE ARGS...: the staff CLI with the file as stdin; its exit code in $cli_exit,
# its stdout (ids only) in $work/cli.out.
cli() {
  local secret_file="$1"
  shift
  cli_exit=0
  bash -c "$MKOS_CLI"' "$@"' mkos "$@" <"$secret_file" >"$work/cli.out" 2>"$work/cli.err" \
    || cli_exit=$?
}

cli_value() {
  tr ' ' '\n' <"$work/cli.out" | sed -n "s/^$1=//p" | head -n 1
}

# login EMAIL PASSWORD_FILE OUT: the token response saved in OUT (0600); the request body comes
# from a file, so the password is on no argv.
login() {
  python3 -c '
import json, sys
with open(sys.argv[2], encoding="utf-8") as fh:
    secret = fh.readline().rstrip("\n")
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump({"email": sys.argv[3], "password": secret}, fh)
' "$work/login.json" "$2" "$1"
  call POST /auth/login "" "" "$work/login.json"
  rm -f "$work/login.json"
  cp "$body" "$3"
}

# login_device EMAIL PASSWORD_FILE DEVICE_TOKEN_FILE OUT: a login that also sends the browser's
# trusted-browser token (ADR-0023) from a file, so no secret is on an argv.
login_device() {
  python3 -c '
import json, sys
with open(sys.argv[2], encoding="utf-8") as fh:
    secret = fh.readline().rstrip("\n")
with open(sys.argv[4], encoding="utf-8") as fh:
    device = fh.readline().rstrip("\n")
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump({"email": sys.argv[3], "password": secret, "device_token": device}, fh)
' "$work/login.json" "$2" "$1" "$3"
  call POST /auth/login "" "" "$work/login.json"
  rm -f "$work/login.json"
  cp "$body" "$4"
}

# token FILE KEY: one value of a saved token response.
token() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]])' "$1" "$2"
}

# refresh_body FILE OUT: {"refresh_token": <FILE's refresh token>} in OUT.
refresh_body() {
  python3 -c '
import json, sys
token = json.load(open(sys.argv[1], encoding="utf-8"))["refresh_token"]
json.dump({"refresh_token": token}, open(sys.argv[2], "w", encoding="utf-8"))
' "$1" "$2"
}

# claim FILE KEY: one claim of the saved response's access token (no signature check: the
# server checked it; this only reads the tenant id it issued).
claim() {
  python3 -c '
import base64, json, sys
token = json.load(open(sys.argv[1], encoding="utf-8"))["access_token"]
part = token.split(".")[1]
print(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))[sys.argv[2]])
' "$1" "$2"
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "$base/readyz")" || status="000"
expect 200

step setup_tenant
umask 077
new_password "$work/user.password"
cli "$work/user.password" tenant create --name "E2E Trust $run" --slug "$slug" \
  --owner-email "$user_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant create exited $cli_exit"
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' >"$work/device.token"

step login_with_token_off
login_device "$user_email" "$work/user.password" "$work/device.token" "$work/s1.json"
expect 200
cp "$work/s1.json" "$body"
json 'b["status"] == "signed_in" and b.get("device_token") is None and b.get("device_token_invalid") is None and b.get("device_trust_days") is None' >/dev/null
first="$(token "$work/s1.json" access_token)"

step list_empty_200
call GET /auth/devices "$first"
expect 200
json 'b == {"devices": []}' >/dev/null

step remove_unknown_404
call DELETE "/auth/devices/00000000-0000-4000-8000-000000000000" "$first"
expect 404
json 'b["error"]["type"] == "NOT_FOUND"' >/dev/null

step second_session_200
login "$user_email" "$work/user.password" "$work/s2.json"
expect 200
second="$(token "$work/s2.json" access_token)"

step sign_out_all_204
call POST /auth/devices/sign-out-all "$second"
expect 204

step other_session_ended_401
refresh_body "$work/s1.json" "$work/r1.json"
call POST /auth/refresh "" "" "$work/r1.json"
expect 401

step this_session_kept_200
refresh_body "$work/s2.json" "$work/r2.json"
call POST /auth/refresh "" "" "$work/r2.json"
expect 200

echo "e2e: trusted_devices OK"
