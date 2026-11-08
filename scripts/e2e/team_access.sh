#!/usr/bin/env bash
# Team access for brand-ready v1 (SI5, ADR-0029), HTTP only after the staff CLI setup. Local runs use
# MFA_ENFORCEMENT=off (the default), so step-up is a no-op and every reset link is refused (PLAN
# D8); the redeem under `all` is covered by the integration suite (test_password_reset_links.py).
#
#   cli_trial_tenant_and_admin  the staff CLI: a fresh trial tenant with its owner, and a run-unique
#                               staff tenant whose owner is made a platform admin
#   owner_invites_201           the owner invites a reviewer and an admin (copied links)
#   invites_list_pending_200    both listed as pending, no token in the list
#   lookup_new_200              the reviewer's link previews the brand, the role and `account: new`
#   accept_password_200         the reviewer creates the account with a password
#   reviewer_login_200          the new member signs in
#   accept_replay_404           the same link again is the generic 404
#   reset_link_off_403          a reset link for the reviewer is refused under `off`
#   admin_lists_trial_200       the platform admin finds the fresh tenant among the trials
#   extend_trial_200            30 more days, still `trial`, a later end
#   activate_200                trial -> active; again -> the same row; listed as active
#   owner_login_again_200       the owner still signs in
#
# Usage: MKOS_CLI=<cmd prefix> scripts/e2e/team_access.sh [base_url]
# Local only: any other host exits 2 before a request. Rerunnable: run-unique emails and slugs. On
# failure it names the step and the HTTP status seen, never a body. Tokens and passwords stay in a
# 0700 temp dir and never reach an argv or the log.
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
slug="e2e-team-$run"
staff_slug="e2e-tstaff-$run"
owner_email="team-owner-$run@example.test"
admin_email="team-padmin-$run@example.test"
reviewer_email="team-reviewer-$run@example.test"
invited_admin="team-admin-$run@example.test"
staff_email="staff@example.test"

work="$(mktemp -d)"
chmod 0700 "$work"
trap 'rm -rf "$work"' EXIT
umask 077
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

# call METHOD PATH [TOKEN] [JSON_FILE]: the status in $status, the body in $body. JSON bodies come
# from files only, so no secret reaches an argv.
call() {
  local method="$1" path="$2" token="${3:-}" data="${4:-}"
  local args=(-s -o "$body" -w '%{http_code}' --max-time 60 -X "$method" -H 'Accept: application/json')
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$data" ]; then args+=(-H 'Content-Type: application/json' --data-binary "@$data"); fi
  status="$(curl "${args[@]}" "$api$path")" || status="000"
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

# login EMAIL PASSWORD_FILE OUT: POST /auth/login; the answer is copied to OUT.
login() {
  python3 -c '
import json, sys
with open(sys.argv[2], encoding="utf-8") as fh:
    secret = fh.readline().rstrip("\n")
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump({"email": sys.argv[3], "password": secret}, fh)
' "$work/login.json" "$2" "$1"
  call POST /auth/login "" "$work/login.json"
  rm -f "$work/login.json"
  cp "$body" "$3"
}

# token FILE: the access token of a saved sign-in answer.
token() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["access_token"])' "$1"
}

# body_file OUT JSON: a request body written to a file (no secret in it).
body_file() {
  printf '%s' "$2" >"$1"
}

# fragment OUT: the `#token=` of the last answer's `invite_url`, kept in a file only.
fragment() {
  python3 -c '
import json, sys
url = json.load(open(sys.argv[1]))["invite_url"]
open(sys.argv[2], "w").write(url.split("#token=", 1)[1])' "$body" "$1"
}

# listed ID: whether the last tenant page holds ID (a plain test, never a step failure).
listed() {
  python3 -c '
import json, sys
rows = json.load(open(sys.argv[1]))["data"]
raise SystemExit(0 if any(t["id"] == sys.argv[2] for t in rows) else 1)' "$body" "$1"
}

# token_body OUT TOKEN_FILE [PASSWORD_FILE]: {"token": ..., "password"?: ...} from files only.
token_body() {
  python3 -c '
import json, sys
out = {"token": open(sys.argv[2]).read().strip()}
if len(sys.argv) > 3:
    out["password"] = open(sys.argv[3]).readline().rstrip("\n")
json.dump(out, open(sys.argv[1], "w"))' "$@"
}

step health
status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "$base/readyz")" || status="000"
expect 200

step cli_trial_tenant_and_admin
new_password "$work/owner.password"
new_password "$work/admin.password"
cli "$work/owner.password" tenant create --name "E2E Team $run" --slug "$slug" \
  --owner-email "$owner_email" --staff-email "$staff_email" --trial-days 14
[ "$cli_exit" -eq 0 ] || fail "tenant create (trial) exited $cli_exit"
tenant_id="$(sed -n 's/^tenant_id=\([^ ]*\) .*/\1/p' "$work/cli.out" | tail -n 1)"
[ -n "$tenant_id" ] || fail "no tenant id from the CLI"
cli "$work/admin.password" tenant create --name "E2E Team Staff $run" --slug "$staff_slug" \
  --owner-email "$admin_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant create (staff) exited $cli_exit"
cli /dev/null user make-admin --email "$admin_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "user make-admin exited $cli_exit"

step owner_invites_201
login "$owner_email" "$work/owner.password" "$work/owner.json"
expect 200
owner="$(token "$work/owner.json")"
body_file "$work/invite1.json" '{"email": "'"$reviewer_email"'", "role": "reviewer"}'
call POST /invites "$owner" "$work/invite1.json"
expect 201
json 'b["invite"]["status"] == "pending" and "#token=" in b["invite_url"]' >/dev/null
fragment "$work/reviewer.token"
body_file "$work/invite2.json" '{"email": "'"$invited_admin"'", "role": "admin"}'
call POST /invites "$owner" "$work/invite2.json"
expect 201

step invites_list_pending_200
call GET "/invites?status=pending" "$owner"
expect 200
json 'len(b["data"]) == 2 and all(i["status"] == "pending" for i in b["data"]) and all("token" not in i for i in b["data"])' >/dev/null

step lookup_new_200
token_body "$work/lookup.json" "$work/reviewer.token"
call POST /invites/lookup "" "$work/lookup.json"
expect 200
json 'b["account"] == "new" and b["role"] == "reviewer" and b["tenant_name"] == "E2E Team '"$run"'"' >/dev/null

step accept_password_200
new_password "$work/reviewer.password"
token_body "$work/accept.json" "$work/reviewer.token" "$work/reviewer.password"
call POST /invites/accept "" "$work/accept.json"
expect 200
json 'b["email"] == "'"$reviewer_email"'"' >/dev/null

step reviewer_login_200
login "$reviewer_email" "$work/reviewer.password" "$work/reviewer.json"
expect 200

step accept_replay_404
call POST /invites/accept "" "$work/accept.json"
expect 404
rm -f "$work/accept.json"

step reset_link_off_403
call GET /members "$owner"
expect 200
reviewer_id="$(json '[m["user_id"] for m in b["data"] if m["email"] == "'"$reviewer_email"'"][0]')"
call POST "/members/$reviewer_id/password-reset" "$owner"
expect 403
json 'b["error"]["type"] == "PERMISSION_DENIED"' >/dev/null

step admin_lists_trial_200
login "$admin_email" "$work/admin.password" "$work/padmin.json"
expect 200
padmin="$(token "$work/padmin.json")"
found=""
cursor=""
for _page in 1 2 3; do
  call GET "/admin/tenants?status=trial&limit=50${cursor:+&cursor=$cursor}" "$padmin"
  expect 200
  if listed "$tenant_id"; then
    found="yes"
    trial_end="$(json '[t["trial_ends_at"] for t in b["data"] if t["id"] == "'"$tenant_id"'"][0]')"
    break
  fi
  cursor="$(json 'b["page"]["next_cursor"]')"
  [ -n "$cursor" ] || break
done
[ "$found" = "yes" ] || fail "the fresh trial tenant is not listed"

step extend_trial_200
body_file "$work/extend.json" '{"days": 30, "reason": "e2e pilot"}'
call POST "/admin/tenants/$tenant_id/extend-trial" "$padmin" "$work/extend.json"
expect 200
json 'b["status"] == "trial" and b["trial_ends_at"] > "'"$trial_end"'"' >/dev/null

step activate_200
body_file "$work/activate.json" '{"reason": "e2e paid"}'
call POST "/admin/tenants/$tenant_id/activate" "$padmin" "$work/activate.json"
expect 200
json 'b["status"] == "active"' >/dev/null
call POST "/admin/tenants/$tenant_id/activate" "$padmin" "$work/activate.json"
expect 200
json 'b["status"] == "active" and b["id"] == "'"$tenant_id"'"' >/dev/null
call GET "/admin/tenants?status=active&limit=50" "$padmin"
expect 200

step owner_login_again_200
login "$owner_email" "$work/owner.password" "$work/owner2.json"
expect 200

echo "e2e: team_access OK"
