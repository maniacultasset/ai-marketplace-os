#!/usr/bin/env bash
# Members, HTTP only after the staff CLI setup (S26 T22; AC 10): in a fresh tenant the owner lists
# the members, changes the operator to reviewer (200); that user may not manage members (403); the
# owner deactivates them (200), after which their refresh and their login are 401; the owner
# reactivates them (200) and they log in again (200); an admin may not change the owner (403);
# the owner restores the operator role at the end (the tenant's roles are as created).
#
# Usage: MKOS_CLI=<cmd prefix> scripts/e2e/members.sh [base_url]
# Setup is the only non-HTTP part: `tenant create` and two `tenant add-member` with run-unique
# slugs and emails (the seed users are never touched). Passwords are made per run and reach the
# CLI and the login only through files. At most 3 logins per email; no poll. Local only: any other
# host exits 2 before a request. On failure it prints the step name and the HTTP status seen,
# never a body.
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
slug="e2e-mem-$run"
owner_email="mem-owner-$run@example.test"
member_email="mem-operator-$run@example.test"
admin_email="mem-admin-$run@example.test"
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

# patch TOKEN USER_ID JSON: PATCH /members/USER_ID with the JSON body.
patch() {
  printf '%s' "$3" >"$work/patch.json"
  call PATCH "/members/$2" "$1" "" "$work/patch.json"
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "$base/readyz")" || status="000"
expect 200

step cli_tenant_and_members
umask 077
for who in owner member admin; do new_password "$work/$who.password"; done
cli "$work/owner.password" tenant create --name "E2E Members $run" --slug "$slug" \
  --owner-email "$owner_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant create exited $cli_exit"
cli "$work/member.password" tenant add-member --slug "$slug" --email "$member_email" \
  --role operator --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant add-member (operator) exited $cli_exit"
cli "$work/admin.password" tenant add-member --slug "$slug" --email "$admin_email" \
  --role admin --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant add-member (admin) exited $cli_exit"

step login_owner
login "$owner_email" "$work/owner.password" "$work/owner.json"
expect 200
owner="$(token "$work/owner.json" access_token)"

step list_members_200
call GET "/members?limit=200" "$owner"
expect 200
json 'sorted((m["email"], m["role"], m["is_active"]) for m in b["data"]) == sorted([("'"$owner_email"'", "owner", True), ("'"$member_email"'", "operator", True), ("'"$admin_email"'", "admin", True)])' >/dev/null
member_id="$(json '[m["user_id"] for m in b["data"] if m["email"] == "'"$member_email"'"][0]')"
owner_id="$(json '[m["user_id"] for m in b["data"] if m["email"] == "'"$owner_email"'"][0]')"
admin_id="$(json '[m["user_id"] for m in b["data"] if m["email"] == "'"$admin_email"'"][0]')"

step patch_operator_to_reviewer_200
patch "$owner" "$member_id" '{"role": "reviewer"}'
expect 200
json 'b["role"] == "reviewer" and b["is_active"] is True' >/dev/null

step login_member
login "$member_email" "$work/member.password" "$work/member.json"
expect 200
[ "$(claim "$work/member.json" roles)" = "['reviewer']" ] || fail "the new login does not carry the reviewer role"

step member_patch_403
patch "$(token "$work/member.json" access_token)" "$admin_id" '{"role": "operator"}'
expect 403
json 'b["error"]["type"] == "PERMISSION_DENIED"' >/dev/null

step deactivate_200
patch "$owner" "$member_id" '{"is_active": false}'
expect 200
json 'b["is_active"] is False' >/dev/null

step deactivated_refresh_401
refresh_body "$work/member.json" "$work/member_refresh.json"
call POST /auth/refresh "" "" "$work/member_refresh.json"
expect 401

step deactivated_login_401
login "$member_email" "$work/member.password" "$work/member2.json"
expect 401

step reactivate_200
patch "$owner" "$member_id" '{"is_active": true}'
expect 200
json 'b["is_active"] is True' >/dev/null

step reactivated_login_200
login "$member_email" "$work/member.password" "$work/member3.json"
expect 200

step login_admin
login "$admin_email" "$work/admin.password" "$work/admin.json"
expect 200

step admin_patch_owner_403
patch "$(token "$work/admin.json" access_token)" "$owner_id" '{"role": "operator"}'
expect 403

step owner_unchanged
call GET "/members?limit=200" "$owner"
expect 200
json 'any(m["user_id"] == "'"$owner_id"'" and m["role"] == "owner" and m["is_active"] for m in b["data"])' >/dev/null

step restore_operator_role_200
patch "$owner" "$member_id" '{"role": "operator"}'
expect 200
json 'b["role"] == "operator" and b["is_active"] is True' >/dev/null

echo "e2e: members OK"
