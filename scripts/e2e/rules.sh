#!/usr/bin/env bash
# The S14b rules e2e, HTTP only (S14b T15): an acme owner lists the rules (a platform row among
# them), dry-runs a 221-character title (FIELD_TOO_LONG from the seeded Amazon title rule) and a
# compliant one (passes); no token is 401; a reviewer may not dry-run (403); an unknown marketplace
# and category pair is 422. Two logins per run. Read-only: the dry-run saves nothing.
#
# Usage: scripts/e2e/rules.sh [base_url]   default http://127.0.0.1:<API_HOST_PORT>
# Local only (the demo seed's public password): any other host exits 2 before a request.
# Needs the compose stack with the demo seed (`make dev` + `make seed`).
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
login() {
  call POST /auth/login "" "{\"email\": \"$1\", \"password\": \"$password\"}"
  expect 200
  json 'b["access_token"]'
}

# draft TITLE [MARKETPLACE] [CATEGORY]: a dry-run body for a kurta set with both required facts.
draft() {
  local title="$1" marketplace="${2:-amazon}" category="${3:-ethnic_wear_kurta_sets}"
  python3 - "$title" "$marketplace" "$category" <<'PY'
import json, sys, uuid
title, marketplace, category = sys.argv[1:4]
print(json.dumps({"listing": {
    "product_id": str(uuid.uuid4()),
    "marketplace": marketplace,
    "category": category,
    "content": {"title": title, "bullets": ["Soft cotton fabric"], "description": "A kurta set."},
    "attributes": {"color": "indigo", "fabric_type": "cotton"},
}}))
PY
}

unit="Cotton Kurta Set for Women with Straight Fit and Round Neck "
# title LENGTH: the unit repeated and cut to LENGTH - 1 characters, plus a full stop.
title() { python3 -c 'import sys; n = int(sys.argv[2]) - 1; print((sys.argv[1] * 4)[:n] + ".")' "$unit" "$1"; }
long_title="$(title 221)"
short_title="$(title 200)"

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step login_acme_owner
owner="$(login owner@acme.test)"

step rules_list_200
call GET "/rules?limit=200" "$owner"
expect 200
json 'any(r["scope"] == "platform" for r in b["data"])' >/dev/null

step dry_run_221_fails
call POST /rules/dry-run "$owner" "$(draft "$long_title")"
expect 200
json 'b["passed"] is False and [(e["error_code"], e["field"]) for e in b["report"]["errors"]] == [("FIELD_TOO_LONG", "content.title")]' >/dev/null

step dry_run_200_passes
call POST /rules/dry-run "$owner" "$(draft "$short_title")"
expect 200
json 'b["passed"] is True and b["report"]["errors"] == []' >/dev/null

step no_token_401
call POST /rules/dry-run "" "$(draft "$short_title")"
expect 401

step login_acme_reviewer
reviewer="$(login reviewer@acme.test)"

step reviewer_dry_run_403
call POST /rules/dry-run "$reviewer" "$(draft "$short_title")"
expect 403
json 'b["error"]["type"] == "PERMISSION_DENIED"' >/dev/null

step unknown_pair_422
call POST /rules/dry-run "$owner" "$(draft "$short_title" flipkart ethnic_wear_jackets)"
expect 422
json 'b["error"]["type"] == "VALIDATION_FAILED"' >/dev/null

echo "e2e: rules OK"
