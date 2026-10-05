#!/usr/bin/env bash
# contracts gate: hash-pinned pydantic runtime, editable mkos-schemas, golden fixtures (S2), then
# the oasdiff breaking-change check on openapi.json against the base branch (S3).
set -euo pipefail
cd "$(dirname "$0")/../.."

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
venv="$work/venv"

uv venv --python 3.13 "$venv"
uv pip install --python "$venv/bin/python" --require-hashes -r build-constraints.txt
uv export --frozen --package mkos-schemas --no-dev --no-emit-workspace --format requirements-txt \
  -o "$work/runtime.txt"
grep -q '^pydantic==' "$work/runtime.txt"
uv pip install --python "$venv/bin/python" --require-hashes -r "$work/runtime.txt"
PIP_DISABLE_PIP_VERSION_CHECK=1 "$venv/bin/pip" install --no-build-isolation --no-index -e packages/schemas
(
  cd "$work"
  "$venv/bin/python" -c "from mkos_schemas.listing import CanonicalListing"
)
"$venv/bin/python" scripts/ci/contracts_check.py --fixtures packages/schemas/fixtures
echo "contracts: mkos-schemas bare venv, fixtures and pass alias OK"
scripts/ci/oasdiff_check.sh "${CONTRACTS_BASE_REF:-origin/main}"
