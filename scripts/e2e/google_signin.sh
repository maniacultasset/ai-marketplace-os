#!/usr/bin/env bash
# Sign in and sign up with Google (ADR-0020), HTTP only after the staff CLI setup, against the fake
# Google the fake marketplace serves under /google (`make e2e` starts it and runs the local API with
# GOOGLE_* pointed at it, FAKE_MARKETPLACE_ENV). Local runs use MFA_ENFORCEMENT=off; the code step
# after Google is covered by the integration suite.
#
#   setup               the staff CLI: a run-unique staff tenant whose owner is made a platform
#                       admin (approves sign-ups), and a vetted brand owner with a Workspace email
#   new_user_*          start -> the fake's consent (a run-unique Gmail) -> callback -> complete:
#                       needs_details with the Google name prefilled; the prefilled sign-up -> 202
#   pending_again       the same Google account again -> pending (no second request)
#   admin_approves      the platform admin approves over HTTP
#   google_sign_in      the same Google account -> sign_in, signed_in; /auth/me; /auth/security
#                       says no password and Google linked
#   link_required       the vetted owner's Workspace account -> link_required; a wrong password is
#                       401 and the handle survives; the right one -> signed_in and linked
#   linked_sign_in      the owner's Google account now signs in directly
#   cancelled           backing out at Google -> ?error=cancelled
#   not_authoritative   an external email Google does not own -> ?error=failed
#   foreign_binding     a handle completed with another browser's binding -> 401 OAUTH_STATE_INVALID
#   flag_off            the flag-off API (FLAG_OFF_URL, when set): start -> /login?error=google_unavailable
#
# Usage: FAKE_URL=<fake> MKOS_CLI=<cmd prefix> scripts/e2e/google_signin.sh [base_url]
# Local only: any other host exits 2 before a request. Rerunnable: run-unique emails and slugs. On
# failure it names the step and the HTTP status seen, never a body. Handles, codes and the binding
# secret stay in a 0700 temp dir and never reach an argv or the log.
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  echo "usage: FAKE_URL=<fake> MKOS_CLI=<cmd prefix> $0 [base_url]" >&2
  exit 2
}
[ -n "${MKOS_CLI:-}" ] && [ -n "${FAKE_URL:-}" ] || usage

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
python3 scripts/ci/local_url.py "$FAKE_URL" || exit 2
base="${base%/}"
api="$base/api/v1"
run="$(date +%s)$RANDOM"
staff_slug="e2e-gstaff-$run"
admin_email="gadmin-$run@example.test"
brand_slug="e2e-gbrand-$run"
owner_domain="gbrand-$run.example.test"
owner_email="owner@$owner_domain"
gmail="asha.$run@gmail.com"
staff_email="staff@example.test"

work="$(mktemp -d)"
chmod 0700 "$work"
trap 'rm -rf "$work"' EXIT
umask 077
body="$work/body.json"
headers="$work/headers.txt"
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

# call METHOD URL [TOKEN] [JSON_FILE]: the status in $status, the body in $body, the headers in
# $headers (redirects are never followed). JSON bodies come from files only.
call() {
  local method="$1" url="$2" token="${3:-}" data="${4:-}"
  local args=(-s -o "$body" -D "$headers" -w '%{http_code}' --max-time 60 -X "$method")
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$data" ]; then args+=(-H 'Content-Type: application/json' --data-binary "@$data"); fi
  status="$(curl "${args[@]}" "$url")" || status="000"
}

# location: the Location header of the last answer.
location() {
  sed -n 's/^[Ll]ocation: *//p' "$headers" | tr -d '\r' | head -n 1
}

# json EXPR: a Python expression over the parsed body `b`; a false or failing one fails the step.
json() {
  python3 - "$body" "$1" <<'PY' || fail "unexpected response shape"
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        b = json.load(fh)
    scope = {"__builtins__": {}, "b": b, "len": len, "str": str, "all": all, "any": any}
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
  python3 -c 'import secrets; print("%039d-Kq" % secrets.randbits(128))' >"$1"
}

cli() {
  local secret_file="$1"
  shift
  cli_exit=0
  bash -c "$MKOS_CLI"' "$@"' mkos "$@" <"$secret_file" >"$work/cli.out" 2>"$work/cli.err" \
    || cli_exit=$?
}

# browser NAME: a fresh binding secret in $work/NAME.secret and its sha256 (b64url) in NAME.b
browser() {
  python3 - "$work/$1" <<'PY'
import base64, hashlib, secrets, sys
secret = secrets.token_urlsafe(32)
open(sys.argv[1] + ".secret", "w").write(secret)
digest = hashlib.sha256(secret.encode()).digest()
open(sys.argv[1] + ".b", "w").write(base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
PY
}

# query_value URL KEY: one query parameter of a URL.
query_value() {
  python3 -c '
import sys
from urllib.parse import parse_qs, urlsplit
print(parse_qs(urlsplit(sys.argv[1]).query).get(sys.argv[2], [""])[0])' "$1" "$2"
}

# google BROWSER CONTROL: start -> the fake consent (CONTROL: e.g. "as=x@gmail.com") -> callback;
# the callback's Location is left in $work/back.txt.
google() {
  local name="$1" control="$2" consent back
  call GET "$api/auth/google/start?b=$(cat "$work/$name.b")"
  [ "$status" = "302" ] || fail "start answered $status"
  consent="$(location)&$control"
  call GET "$consent"
  [ "$status" = "302" ] || fail "the fake consent answered $status"
  back="$(location)"
  call GET "$api/auth/google/callback?${back#*\?}"
  [ "$status" = "303" ] || fail "the callback answered $status"
  location >"$work/back.txt"
}

# complete BROWSER [BINDING_OF]: POST /auth/google/complete with the handle in $work/back.txt
complete() {
  python3 - "$work/back.txt" "$work/${2:-$1}.secret" "$work/complete.json" <<'PY'
import json, sys
from urllib.parse import parse_qs, urlsplit
handle = parse_qs(urlsplit(open(sys.argv[1]).read().strip()).query)["handle"][0]
json.dump({"handle": handle, "binding": open(sys.argv[2]).read()}, open(sys.argv[3], "w"))
PY
  call POST "$api/auth/google/complete" "" "$work/complete.json"
}

# with_secret BROWSER KEY OUT EXTRA_JSON: {KEY: <the last body's handle>, binding, ...EXTRA}
handle_body() {
  python3 - "$body" "$work/$1.secret" "$2" "$3" "$4" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
section = b["details"] if sys.argv[3] == "details_handle" else b["link"]
out = {sys.argv[3]: section[sys.argv[3]], "binding": open(sys.argv[2]).read()}
out.update(json.loads(sys.argv[5]))
json.dump(out, open(sys.argv[4], "w"))
PY
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "$base/readyz")" || status="000"
expect 200

step cli_admin_and_vetted_owner
new_password "$work/admin.password"
new_password "$work/owner.password"
cli "$work/admin.password" tenant create --name "E2E Google Staff $run" --slug "$staff_slug" \
  --owner-email "$admin_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant create (staff) exited $cli_exit"
cli /dev/null user make-admin --email "$admin_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "user make-admin exited $cli_exit"
cli "$work/owner.password" tenant create --name "E2E Google Brand $run" --slug "$brand_slug" \
  --owner-email "$owner_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant create (brand) exited $cli_exit"

step new_user_needs_details
browser tab1
google tab1 "as=$gmail&name=Asha%20Rao"
[ "$(query_value "$(cat "$work/back.txt")" handle)" != "" ] || fail "no handle"
complete tab1
expect 200
json 'b["outcome"] == "needs_details" and b["details"]["email"] == "'"$gmail"'" and b["details"]["email_verified"] and b["details"]["owner_name"] == "Asha Rao"' >/dev/null

step new_user_signup_202
handle_body tab1 details_handle "$work/signup.json" '{"brand_name": "Asha Botanicals '"$run"'", "owner_name": "Asha Rao"}'
call POST "$api/signup/google" "" "$work/signup.json"
expect 202
json 'b["status"] == "pending"' >/dev/null
request_id="$(json 'b["request_id"]')"

step pending_again
google tab1 "as=$gmail"
complete tab1
expect 200
json 'b["outcome"] == "pending" and b["signup"]["request_id"] == "'"$request_id"'"' >/dev/null

step admin_approves
python3 -c '
import json, sys
json.dump({"email": sys.argv[1], "password": open(sys.argv[2]).readline().rstrip("\n")}, open(sys.argv[3], "w"))
' "$admin_email" "$work/admin.password" "$work/login.json"
call POST "$api/auth/login" "" "$work/login.json"
rm -f "$work/login.json"
expect 200
admin_token="$(json 'b["access_token"]')"
printf '{}' >"$work/approve.json"
call POST "$api/admin/signup-requests/$request_id/approve" "$admin_token" "$work/approve.json"
expect 200
json 'b["status"] == "approved" and b["verified_via"] == "google"' >/dev/null

step google_sign_in
google tab1 "as=$gmail"
complete tab1
expect 200
json 'b["outcome"] == "sign_in" and b["login"]["status"] == "signed_in"' >/dev/null
access="$(json 'b["login"]["access_token"]')"
call GET "$api/auth/me" "$access"
expect 200
json 'b["email"] == "'"$gmail"'" and b["role"] == "owner"' >/dev/null
call GET "$api/auth/security" "$access"
expect 200
json 'b["has_password"] is False and b["google_linked"] is True' >/dev/null

step link_required
browser tab2
google tab2 "as=$owner_email&hd=$owner_domain"
complete tab2
expect 200
json 'b["outcome"] == "link_required" and b["link"]["email"] == "'"$owner_email"'"' >/dev/null
handle_body tab2 link_handle "$work/link_wrong.json" '{"password": "Not-The-Password-0"}'
python3 - "$work/link_wrong.json" "$work/owner.password" "$work/link.json" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
b["password"] = open(sys.argv[2]).readline().rstrip("\n")
json.dump(b, open(sys.argv[3], "w"))
PY
call POST "$api/auth/google/link" "" "$work/link_wrong.json"
expect 401
call POST "$api/auth/google/link" "" "$work/link.json"
expect 200
json 'b["status"] == "signed_in"' >/dev/null
owner_token="$(json 'b["access_token"]')"
call GET "$api/auth/security" "$owner_token"
expect 200
json 'b["has_password"] is True and b["google_linked"] is True' >/dev/null

step linked_sign_in
google tab2 "as=$owner_email&hd=$owner_domain"
complete tab2
expect 200
json 'b["outcome"] == "sign_in" and b["login"]["status"] == "signed_in"' >/dev/null

step cancelled
google tab2 "deny=1"
[ "$(query_value "$(cat "$work/back.txt")" error)" = "cancelled" ] || fail "want ?error=cancelled"

step not_authoritative
google tab2 "as=someone-$run@yahoo.com"
[ "$(query_value "$(cat "$work/back.txt")" error)" = "failed" ] || fail "want ?error=failed"

step foreign_binding
browser tab3
google tab1 "as=$gmail"
complete tab1 tab3
expect 401
json 'b["error"]["type"] == "OAUTH_STATE_INVALID"' >/dev/null

step flag_off
if [ -n "${FLAG_OFF_URL:-}" ]; then
  python3 scripts/ci/local_url.py "$FLAG_OFF_URL" || exit 2
  call GET "${FLAG_OFF_URL%/}/api/v1/auth/google/start?b=$(cat "$work/tab3.b")"
  expect 302
  [ "$(location)" = "/login?error=google_unavailable" ] || fail "want /login?error=google_unavailable"
else
  echo "e2e: SKIP flag_off (no FLAG_OFF_URL)"
fi

echo "e2e: google_signin OK"
