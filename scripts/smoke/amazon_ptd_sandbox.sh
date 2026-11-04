#!/usr/bin/env bash
# DW2-S: the manual sandbox smoke of the PTD refresh (never CI). Fetches the KURTA definition from
# the SP-API sandbox under the sandbox app's refresh token, as a dry run (no database), saves the
# verified schema to a temp folder outside the repo, runs `templates diff` on it, and fails if any
# output holds a token, an ARN-like id, a pre-signed parameter or a URL.
#
# Needs ~/kuinn-amazon-sandbox.env with AMAZON_CLIENT_ID, AMAZON_CLIENT_SECRET and
# AMAZON_REFRESH_TOKEN. No value is ever printed.
set -euo pipefail

env_file="${HOME}/kuinn-amazon-sandbox.env"
[ -r "$env_file" ] || { echo "amazon_ptd_sandbox: $env_file is missing" >&2; exit 1; }
api_dir="$(cd "$(dirname "$0")/../../apps/api" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

(
  set -a
  # shellcheck disable=SC1090
  . "$env_file"
  set +a
  export AMAZON_PLATFORM_REFRESH_TOKEN="$AMAZON_REFRESH_TOKEN"
  unset AMAZON_REFRESH_TOKEN
  export AMAZON_USE_SANDBOX=true
  export AMAZON_SPAPI_ENDPOINT=https://sandbox.sellingpartnerapi-eu.amazon.com
  export APP_ENV=local APP_ROLE=shell
  cd "$api_dir"
  uv run --frozen python -m app.cli templates refresh --product-type KURTA --dry-run \
    --save-schema "$tmp/KURTA.json" --staff-email smoke@example.com \
    --reason "DW2-S sandbox smoke"
  uv run --frozen python -m app.cli templates diff --product-type KURTA \
    --from-file "$tmp/KURTA.json" || [ $? -eq 1 ]
) >"$tmp/out.txt" 2>&1 || { tail -n 5 "$tmp/out.txt" >&2; exit 1; }

if grep -Eq 'Atza\||Atzr\||amzn1\.|X-Amz-|https://' "$tmp/out.txt"; then
  echo "amazon_ptd_sandbox: the output holds a secret-shaped string" >&2
  exit 1
fi
grep -E '^templates (refresh|diff):|^(unknown_|unfed_|shape )' "$tmp/out.txt" || true
echo "meta-schema keywords in the saved schema:"
jq -r '[.. | objects | keys[]] | unique | map(select(test("^[a-z$][A-Za-z0-9]*$"))) | .[]' \
  "$tmp/KURTA.json" | tr '\n' ' '
echo
