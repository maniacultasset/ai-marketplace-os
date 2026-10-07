#!/usr/bin/env bash
# The S14b admin e2e (S14b T23): the read-only staff admin under `make dev` (APP_ENV=local, where
# the staff member is ADMIN_LOCAL_EMAIL) renders the seeded listing version's title and its rules
# run's FIELD_TOO_LONG, refuses a write (405) and serves the ops queue page; no fetched page holds
# a seeded password hash or account token. HTTP only, except the one `db_*` step, which reads the
# seeded secrets (read-only) so the pages can be searched for them.
#
# Usage: scripts/e2e/admin.sh [base_url]   default http://127.0.0.1:<API_HOST_PORT>
# Local only: any other host exits 2 before a request.
# Needs the compose stack with the demo seed (`make dev` + `make seed`, ADMIN_ENABLED=true).
# On failure it prints the step name and the HTTP status seen, never a response body.
set -euo pipefail
cd "$(dirname "$0")/../.."

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2
base="${base%/}"
admin="$base/internal/admin"
# The demo seed's fixed ids (app/db/seed_ops.py SEED_NAMESPACE and demo_listing_ids()).
seed_namespace="5eed0000-9c1e-4d6a-8a51-0c5a1e5eed00"

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

pages="$work/pages"
mkdir -p "$pages"
fetched=0
secrets="$work/secrets.txt"

# page METHOD PATH: the status goes in $status; every page is kept for the secret search.
page() {
  local method="$1" path="$2"
  fetched=$((fetched + 1))
  body="$pages/$fetched.html"
  status="$(curl -s -o "$body" -w '%{http_code}' -X "$method" "$admin$path")" || status="000"
}

# contains TEXT: the last page holds TEXT literally.
contains() {
  grep -qF -- "$1" "$body" || fail "the page lacks the expected text"
}

expect() {
  if [ "$status" != "$1" ]; then fail "want HTTP $1"; fi
}

# search_pages: no kept page holds any secret from $secrets.
search_pages() {
  local secret
  while IFS= read -r secret; do
    [ -n "$secret" ] || continue
    if grep -rqF -- "$secret" "$pages"; then fail "a page holds a seeded secret"; fi
  done <"$secrets"
}

seed_id() { python3 -c 'import sys, uuid; print(uuid.uuid5(uuid.UUID(sys.argv[1]), sys.argv[2]))' "$seed_namespace" "$1"; }
version_id="$(seed_id acme:demo-listing:v1)"
run_id="$(seed_id acme:demo-listing:rules-run-1)"

step health
status="$(curl -s -o /dev/null -w '%{http_code}' "$base/readyz")" || status="000"
expect 200

step admin_index_200
page GET /
expect 200
contains "AI Marketplace OS staff"

step listing_versions_list_200
page GET /listing-version/list
expect 200

step seeded_version_detail_has_title
page GET "/listing-version/details/$version_id"
expect 200
contains "Cotton Kurta Set for Women with Straight Fit and Round Neck"

step seeded_run_detail_has_field_too_long
page GET "/validation-run/details/$run_id"
expect 200
contains "FIELD_TOO_LONG"

step db_read_seeded_secrets_then_grep
docker compose exec -T postgres psql -U postgres -d marketplace_os -tA -c \
  "SELECT hashed_password FROM users WHERE hashed_password IS NOT NULL UNION ALL SELECT encode(token_enc, 'hex') FROM marketplace_accounts WHERE token_enc IS NOT NULL UNION ALL SELECT encode(token_enc, 'base64') FROM marketplace_accounts WHERE token_enc IS NOT NULL" \
  >"$secrets" || fail "the database read failed"
[ -s "$secrets" ] || fail "no seeded secret to search for"
search_pages


step post_refused_405
page POST /listing/create
expect 405

step ops_queue_200
page GET /ops/queue
expect 200
contains 'data-ops="queue"'

# the pages fetched after the db step, searched with the same secrets (no second database read)
search_pages

echo "e2e: admin OK ($fetched pages)"
