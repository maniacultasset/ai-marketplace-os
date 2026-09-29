#!/usr/bin/env bash
# The `contracts` check, reduced per ADR-0013 §4 until S2/S3 (fixtures, then oasdiff): a bare
# venv installs packages/schemas and imports it (CANON.md:138). Nothing is fetched unpinned: the venv
# is not seeded (no latest pip), build tools come hash-checked from build-constraints.txt, and the
# editable install builds offline with that hatchling. `--no-index` holds only while mkos-schemas
# has no runtime dependencies; S2 revisits it when pydantic arrives.
set -euo pipefail
cd "$(dirname "$0")/../.."

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
venv="$work/venv"

uv venv --python 3.13 "$venv"
uv pip install --python "$venv/bin/python" --require-hashes -r build-constraints.txt
PIP_DISABLE_PIP_VERSION_CHECK=1 "$venv/bin/pip" install --no-build-isolation --no-index -e packages/schemas
(cd "$work" && "$venv/bin/python" -c "import mkos_schemas")
echo "contracts: mkos-schemas installs into a bare venv and imports"
