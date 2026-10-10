#!/usr/bin/env bash
# Sessions and the tenant switch, HTTP only after the staff CLI setup (S26 T22; AC 10): a user
# logs in, reads /auth/me, rotates the refresh token, and a reuse of the old one is 401 and revokes
# the family (the rotated one is 401 too); after a logout its refresh is 401. Then the switch: the
# same user is a member of two fresh tenants (owner in one, reviewer in the other); POST
# /auth/switch-tenant answers 200 with the other tenant's `tid` and a new refresh family, /auth/me
# shows that tenant and role, the pre-switch access token still reads only the first tenant, and
# the pre-switch refresh token still rotates in its own family (first tenant).
#
# Usage: MKOS_CLI=<cmd prefix> scripts/e2e/auth_session.sh [base_url]
# MKOS_CLI runs `python -m app.cli` as migrator (`make e2e` sets it; arguments appended, a new
# user's password on stdin). Setup is the only non-HTTP part: two `tenant create` and one
# `tenant add-member`, with run-unique slugs and emails (the seed users are never touched), so
# reruns never collide. Passwords are made per run and reach the CLI and the login only through
# files. At most 3 logins for the one email; no poll. Local only: any other host exits 2 before a
# request. On failure it prints the step name and the HTTP status seen, never a body.
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
first_slug="e2e-sess-a-$run"
second_slug="e2e-sess-b-$run"
user_email="session-$run@example.test"
other_owner="session-owner-$run@example.test"
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

new_password() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(24))' >"$1"
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

step cli_two_tenants_one_user
umask 077
new_password "$work/user.password"
new_password "$work/other.password"
cli "$work/user.password" tenant create --name "E2E Session A $run" --slug "$first_slug" \
  --owner-email "$user_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant create (first) exited $cli_exit"
first_tenant="$(cli_value tenant_id)"
cli "$work/other.password" tenant create --name "E2E Session B $run" --slug "$second_slug" \
  --owner-email "$other_owner" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant create (second) exited $cli_exit"
second_tenant="$(cli_value tenant_id)"
cli /dev/null tenant add-member --slug "$second_slug" --email "$user_email" --role reviewer \
  --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant add-member exited $cli_exit"

step login_200
login "$user_email" "$work/user.password" "$work/s1.json"
expect 200
[ "$(claim "$work/s1.json" tid)" = "$first_tenant" ] || fail "the login is not in the first tenant"
access="$(token "$work/s1.json" access_token)"

step me_200
call GET /auth/me "$access"
expect 200
json 'b["email"] == "'"$user_email"'" and b["tenant_id"] == "'"$first_tenant"'" and b["role"] == "owner" and len(b["memberships"]) == 2' >/dev/null

step refresh_rotates_200
refresh_body "$work/s1.json" "$work/r1.json"
call POST /auth/refresh "" "" "$work/r1.json"
expect 200
cp "$body" "$work/s2.json"
[ "$(token "$work/s2.json" refresh_token)" != "$(token "$work/s1.json" refresh_token)" ] \
  || fail "the refresh token did not rotate"

step refresh_reuse_401
call POST /auth/refresh "" "" "$work/r1.json"
expect 401
json 'b["error"]["type"] == "UNAUTHENTICATED"' >/dev/null

step reuse_revoked_family_401
refresh_body "$work/s2.json" "$work/r2.json"
call POST /auth/refresh "" "" "$work/r2.json"
expect 401

step login_again_200
login "$user_email" "$work/user.password" "$work/s3.json"
expect 200

step logout_204
refresh_body "$work/s3.json" "$work/r3.json"
call POST /auth/logout "$(token "$work/s3.json" access_token)" "" "$work/r3.json"
expect 204

step refresh_after_logout_401
call POST /auth/refresh "" "" "$work/r3.json"
expect 401

step login_for_switch_200
login "$user_email" "$work/user.password" "$work/s4.json"
expect 200
before="$(token "$work/s4.json" access_token)"
[ "$(claim "$work/s4.json" tid)" = "$first_tenant" ] || fail "the login is not in the first tenant"

step switch_tenant_200
python3 -c 'import json, sys; json.dump({"target_tenant_id": sys.argv[2]}, open(sys.argv[1], "w"))' \
  "$work/switch.json" "$second_tenant"
call POST /auth/switch-tenant "$before" "" "$work/switch.json"
expect 200
cp "$body" "$work/s5.json"
[ "$(claim "$work/s5.json" tid)" = "$second_tenant" ] || fail "the switch did not issue a token for the second tenant"
[ "$(token "$work/s5.json" refresh_token)" != "$(token "$work/s4.json" refresh_token)" ] \
  || fail "the switch reused the refresh token"

step me_after_switch
call GET /auth/me "$(token "$work/s5.json" access_token)"
expect 200
json 'b["tenant_id"] == "'"$second_tenant"'" and b["role"] == "reviewer"' >/dev/null

step old_access_still_first_tenant
call GET /auth/me "$before"
expect 200
json 'b["tenant_id"] == "'"$first_tenant"'" and b["role"] == "owner"' >/dev/null
call GET /tenants/me "$before"
expect 200
json 'b["id"] == "'"$first_tenant"'"' >/dev/null

step old_refresh_rotates_own_family
refresh_body "$work/s4.json" "$work/r4.json"
call POST /auth/refresh "" "" "$work/r4.json"
expect 200
cp "$body" "$work/s6.json"
[ "$(claim "$work/s6.json" tid)" = "$first_tenant" ] || fail "the old family rotated into another tenant"

step new_family_still_valid
refresh_body "$work/s5.json" "$work/r5.json"
call POST /auth/refresh "" "" "$work/r5.json"
expect 200
cp "$body" "$work/s7.json"
[ "$(claim "$work/s7.json" tid)" = "$second_tenant" ] || fail "the switched family left its tenant"

step switch_to_non_member_404
python3 -c 'import json, sys, uuid; json.dump({"target_tenant_id": str(uuid.uuid4())}, open(sys.argv[1], "w"))' \
  "$work/switch_bad.json"
call POST /auth/switch-tenant "$before" "" "$work/switch_bad.json"
expect 404

echo "e2e: auth_session OK"
