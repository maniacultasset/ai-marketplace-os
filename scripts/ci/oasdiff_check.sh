#!/usr/bin/env bash
# The oasdiff breaking-change check on the committed /api/v1 contract (CANON.md:138; S3 D6).
#
# Usage: scripts/ci/oasdiff_check.sh <base-ref>
# Compares <base-ref>:packages/schemas/jsonschema/openapi.json with the working tree's copy and
# fails on any breaking change at WARN or above (a removed response field, path or status, a
# narrowed type). oasdiff rates a removed optional response field, a removed declared error status
# and a removed optional response header INFO; scripts/ci/oasdiff-levels.txt raises them to WARN,
# because FE and AI consumers read those too. scripts/ci/oasdiff-err-ignore.txt lists breaking
# changes the user approved by name (one line each, METHOD PATH message; T115: ProductCreate.category
# required, 2026-10-06); empty it once main carries them. A base without the file (the PR that first adds it)
# is a logged skip; a base ref that does not resolve is an error, never a skip. The container gets
# no network.
set -euo pipefail
cd "$(dirname "$0")/../.."

base_ref="${1:?usage: oasdiff_check.sh <base-ref>}"
spec="packages/schemas/jsonschema/openapi.json"
# tufin/oasdiff v1.32.1 (the newest release at least 14 days old on 2026-10-05).
image="tufin/oasdiff@sha256:3b14fe0112e5d1bf862f91ab234a4bcd161a3f399e98f8b0b665ce70857694ac"

if ! git rev-parse --verify --quiet "${base_ref}^{commit}" >/dev/null; then
  echo "oasdiff: base ref ${base_ref} not found (fetch it, or set CONTRACTS_BASE_REF)" >&2
  exit 1
fi
if ! git cat-file -e "${base_ref}:${spec}" 2>/dev/null; then
  echo "oasdiff: base has no openapi.json, skipped"
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git show "${base_ref}:${spec}" >"$work/base.json"
cp "$spec" "$work/head.json"
cp scripts/ci/oasdiff-levels.txt "$work/levels.txt"
cp scripts/ci/oasdiff-err-ignore.txt "$work/err-ignore.txt"
chmod -R a+rX "$work"
docker run --rm --network none -v "$work:/specs:ro" "$image" \
  breaking /specs/base.json /specs/head.json --fail-on WARN --severity-levels /specs/levels.txt \
  --err-ignore /specs/err-ignore.txt
echo "oasdiff: no breaking change against ${base_ref}"
