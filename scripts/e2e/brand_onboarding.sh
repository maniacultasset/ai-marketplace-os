#!/usr/bin/env bash
# The S16 brand onboarding, HTTP only apart from the staff CLI and two read-only checks
# (CODE-PLAN S16 T22; AC10): staff create a fresh tenant and its owner with the CLI (a second
# create with the same slug fails), the owner reads the brand profile with its ETag, updates it
# (200) and a stale If-Match is 409; with the worker up a brand guide upload is 202 and its job
# succeeds within 30 s, the list shows it `ready` with chunks, the same bytes under a new key
# answer the same job and the key with another file is 409; a reviewer added by the CLI gets 403
# on upload; bharat cannot see the document (list) or delete it (404); the owner deletes it (204,
# then 404), and the database shows the profile's chunks active and the document's inactive.
#
# Usage: WORKER_START=<cmd> WORKER_STOP=<cmd> MKOS_CLI=<cmd prefix> scripts/e2e/brand_onboarding.sh [base_url]
# WORKER_START/WORKER_STOP are shell commands; MKOS_CLI is a command prefix for `python -m
# app.cli` as migrator (its arguments are appended; a new user's password goes on its stdin).
# Passwords are made per run with `secrets`, and reach the CLI and the login only through stdin
# or a file, never an argv. Every poll gives up after 60 s. Local only: any other host exits 2
# before a request. Rerunnable: every run uses a fresh slug, fresh emails and fresh keys.
# Only the two `db_` steps read the database (`psql`, read-only), and nothing here touches the
# limiter: `make e2e` and the bootstrap reset it before the script.
# On failure it prints the step name and the HTTP status seen, never a response body.
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  echo "usage: WORKER_START=<cmd> WORKER_STOP=<cmd> MKOS_CLI=<cmd prefix> $0 [base_url]" >&2
  exit 2
}
[ -n "${WORKER_START:-}" ] && [ -n "${WORKER_STOP:-}" ] && [ -n "${MKOS_CLI:-}" ] || usage

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
api="$base/api/v1"
seed_password="devpassword123" # the demo seed's local-only password (A§5)
slug="e2e-$(date +%s)-$RANDOM"
owner_email="owner-$slug@example.test"
reviewer_email="reviewer-$slug@example.test"
staff_email="staff@example.test"
guide="scripts/e2e/fixtures/brand_guide.md"
POLL_LIMIT_S=60

work="$(mktemp -d)"
worker_running=0
cleanup() {
  if [ "$worker_running" -eq 1 ]; then bash -c "$WORKER_STOP" || true; fi
  rm -rf "$work"
}
trap cleanup EXIT
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

new_password() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
}
owner_password="$(new_password)"
reviewer_password="$(new_password)"

# cli PASSWORD ARGS...: the staff CLI with PASSWORD as its stdin line; the exit code in $cli_exit,
# its stdout in $work/cli.out (ids only).
cli() {
  local secret="$1"
  shift
  cli_exit=0
  printf '%s\n' "$secret" | bash -c "$MKOS_CLI"' "$@"' mkos "$@" >"$work/cli.out" 2>"$work/cli.err" \
    || cli_exit=$?
}

# cli_value NAME: the `NAME=value` field of the last CLI output.
cli_value() {
  tr ' ' '\n' <"$work/cli.out" | sed -n "s/^$1=//p" | head -n 1
}

# call METHOD PATH [TOKEN] [KEY] [EXTRA_HEADER] [JSON_FILE]: the status goes in $status, the body in
# $body, the response headers in $headers.
call() {
  local method="$1" path="$2" token="${3:-}" key="${4:-}" extra="${5:-}" data="${6:-}"
  local args=(-s -o "$body" -D "$headers" -w '%{http_code}' -X "$method" -H 'Accept: application/json')
  if [ -n "$token" ]; then args+=(-H "Authorization: Bearer $token"); fi
  if [ -n "$key" ]; then args+=(-H "Idempotency-Key: $key"); fi
  if [ -n "$extra" ]; then args+=(-H "$extra"); fi
  if [ -n "$data" ]; then args+=(-H 'Content-Type: application/json' --data-binary "@$data"); fi
  status="$(curl "${args[@]}" "$api$path")" || status="000"
}

# upload TOKEN KEY FILE: the multipart brand document upload.
upload() {
  status="$(curl -s -o "$body" -w '%{http_code}' -X POST -H 'Accept: application/json' \
    -H "Authorization: Bearer $1" -H "Idempotency-Key: $2" \
    -F "file=@$3;type=text/markdown" "$api/brand/documents")" || status="000"
}

# login EMAIL PASSWORD: the body is written to a file, so the password is on no argv.
login() {
  local email="$1" secret="$2"
  printf '%s\n' "$secret" | python3 -c '
import json, sys
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump({"email": sys.argv[2], "password": sys.stdin.readline().rstrip("\n")}, fh)
' "$work/login.json" "$email"
  status="$(curl -s -o "$body" -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    --data-binary "@$work/login.json" "$api/auth/login")" || status="000"
  rm -f "$work/login.json"
  expect 200
  json 'b["access_token"]'
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

# header NAME: the response header's value from the last `call`.
header() {
  tr -d '\r' <"$headers" | awk -F': ' -v want="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" \
    'tolower($1) == want { print $2 }' | tail -n 1
}

# uuid VALUE: VALUE when it is a UUID (it goes into SQL), else the step fails.
uuid() {
  printf '%s' "$1" | grep -Eqx '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
    || fail "not a UUID"
  printf '%s' "$1"
}

# wait_job TOKEN JOB STATUS LIMIT_S: poll the job envelope until STATUS, at most LIMIT_S seconds.
wait_job() {
  local deadline=$(( $(date +%s) + $4 )) seen
  while :; do
    call GET "/jobs/$2" "$1"
    expect 200
    seen="$(json 'b["status"]')"
    if [ "$seen" = "$3" ]; then return 0; fi
    case "$seen" in succeeded|partial|failed|cancelled|dead_letter) fail "the job ended $seen, want $3" ;; esac
    if [ "$(date +%s)" -ge "$deadline" ]; then fail "the job did not reach $3 within $4 s"; fi
    sleep 1
  done
}

printf '# Other guide\n\nA different file for run %s.\n' "$slug" >"$work/other.md"
printf '{"tone": "warm and direct (%s)", "guidelines": ["Lead with fabric and fit."], "glossary": {"kurta outfit": "kurta set"}}\n' \
  "$slug" >"$work/patch.json"
printf '{"tone": "stale write"}\n' >"$work/stale.json"

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step cli_tenant_create
cli "$owner_password" tenant create --name "E2E Brand $slug" --slug "$slug" \
  --owner-email "$owner_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant create exited $cli_exit"
tenant_id="$(uuid "$(cli_value tenant_id)")"

step cli_duplicate_slug_fails
cli "$owner_password" tenant create --name "E2E Brand again" --slug "$slug" \
  --owner-email "$owner_email" --staff-email "$staff_email"
[ "$cli_exit" -eq 4 ] || fail "a second create with the slug exited $cli_exit, want 4"

step login_owner
owner="$(login "$owner_email" "$owner_password")"

step get_tenant_me_etag
call GET /tenants/me "$owner"
expect 200
json 'b["id"] == "'"$tenant_id"'" and b["slug"] == "'"$slug"'"' >/dev/null
etag="$(header etag)"
[ -n "$etag" ] || fail "no ETag"

step patch_tenant_me_200
call PATCH /tenants/me "$owner" "" "If-Match: $etag" "$work/patch.json"
expect 200
json 'b["glossary"] == {"kurta outfit": "kurta set"} and b["guidelines"] == ["Lead with fabric and fit."]' >/dev/null
[ -n "$(header etag)" ] && [ "$(header etag)" != "$etag" ] || fail "the ETag did not change"

step patch_stale_409
call PATCH /tenants/me "$owner" "" "If-Match: $etag" "$work/stale.json"
expect 409
json 'b["error"]["type"] == "STALE_VERSION"' >/dev/null

step worker_start
bash -c "$WORKER_START" || fail "the WORKER_START hook failed"
worker_running=1

step upload_202
key="e2e-brand-$slug"
upload "$owner" "$key" "$guide"
expect 202
job_id="$(json 'b["job_id"]')"

step job_succeeded_under_30s
wait_job "$owner" "$job_id" succeeded 30

step list_ready_with_chunks
call GET /brand/documents "$owner"
expect 200
json 'len(b["data"]) == 1 and b["data"][0]["filename"] == "brand_guide.md" and b["data"][0]["status"] == "ready" and b["data"][0]["chunk_count"] > 0' >/dev/null
document_id="$(uuid "$(json 'b["data"][0]["id"]')")"

step reupload_new_key_same_job
upload "$owner" "e2e-brand-again-$slug" "$guide"
expect 202
json 'b["job_id"] == "'"$job_id"'"' >/dev/null

step key_reuse_other_file_409
upload "$owner" "$key" "$work/other.md"
expect 409
json 'b["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"' >/dev/null

step cli_add_member_reviewer
cli "$reviewer_password" tenant add-member --slug "$slug" --email "$reviewer_email" \
  --role reviewer --staff-email "$staff_email"
[ "$cli_exit" -eq 0 ] || fail "tenant add-member exited $cli_exit"

step login_reviewer
reviewer="$(login "$reviewer_email" "$reviewer_password")"

step reviewer_upload_403
upload "$reviewer" "e2e-brand-reviewer-$slug" "$work/other.md"
expect 403

step login_bharat_reviewer
bharat_reviewer="$(login reviewer@bharat.test "$seed_password")"

step bharat_list_lacks
call GET /brand/documents "$bharat_reviewer"
expect 200
json 'all(d["id"] != "'"$document_id"'" for d in b["data"])' >/dev/null

step login_bharat_admin
bharat_admin="$(login admin@bharat.test "$seed_password")"

step bharat_delete_404
call DELETE "/brand/documents/$document_id" "$bharat_admin"
expect 404

step delete_204
call DELETE "/brand/documents/$document_id" "$owner"
expect 204

step delete_again_404
call DELETE "/brand/documents/$document_id" "$owner"
expect 404

step db_profile_chunks_present
status=""
deadline=$(( $(date +%s) + POLL_LIMIT_S ))
while :; do
  found="$(docker compose exec -T postgres psql -U postgres -d marketplace_os -tAX -v ON_ERROR_STOP=1 -c \
    "SELECT count(*) FROM brand_chunks WHERE tenant_id = '$tenant_id' AND source_type = 'brand_profile' AND source_id = '$tenant_id' AND is_active")" \
    || fail "the profile chunk read failed"
  if [ "${found:-0}" -gt 0 ]; then break; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then fail "no active profile chunks within ${POLL_LIMIT_S} s"; fi
  sleep 1
done

step db_doc_chunks_inactive
counts="$(docker compose exec -T postgres psql -U postgres -d marketplace_os -tAX -v ON_ERROR_STOP=1 -c \
  "SELECT count(*) FILTER (WHERE is_active) || ' ' || count(*) FROM brand_chunks WHERE tenant_id = '$tenant_id' AND source_type = 'brand_document' AND source_id = '$document_id'")" \
  || fail "the document chunk read failed"
read -r active total <<<"$counts"
[ "$active" = "0" ] && [ "${total:-0}" -gt 0 ] || fail "want 0 active of more than 0 document chunks, got $active of $total"

step worker_stop
bash -c "$WORKER_STOP" || fail "the WORKER_STOP hook failed"
worker_running=0

echo "e2e: brand_onboarding OK (tenant $slug)"
