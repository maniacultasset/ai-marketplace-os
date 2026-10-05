#!/usr/bin/env bash
# The release gate and publish step for @maniacultasset/schemas (S3 AC15; `make publish-schemas`).
# Runs only in the publish-schemas workflow on a schemas-v* tag. In order:
#   (1) refuse outside GitHub Actions or on anything but a tag;
#   (2) refuse a tag whose commit is not on origin/main (only reviewed, merged commits publish);
#   (3) regenerate every contract output and refuse any diff;
#   (4) refuse unless tag version == package.json == openapi info.version, and the CHANGELOG
#       heading for that version is closed (not `(unreleased)`);
#   (5) type-check the packed tarball as the FE consumes it;
#   (6) pnpm publish. The token stays in the environment; the npmrc names it, never holds it.
set -euo pipefail
cd "$(dirname "$0")/../.."

fail() { echo "publish: $*" >&2; exit 1; }

# (1)
[ "${GITHUB_ACTIONS:-}" = "true" ] || fail "runs only in GitHub Actions"
[ "${GITHUB_REF_TYPE:-}" = "tag" ] || fail "runs only on a tag push"
tag="${GITHUB_REF_NAME:-}"
case "$tag" in
  schemas-v[0-9]*) ;;
  *) fail "tag '$tag' is not schemas-v<version>" ;;
esac
version="${tag#schemas-v}"
sha="${GITHUB_SHA:?publish: GITHUB_SHA is not set}"

# (2)
git rev-parse --verify --quiet "origin/main^{commit}" >/dev/null || fail "origin/main is not fetched"
git merge-base --is-ancestor "$sha" origin/main || fail "tag commit is not on main"

# (3)
make -C packages/schemas all
git diff --exit-code -- packages/schemas/jsonschema packages/schemas/ts \
  || fail "regenerated contract outputs differ from the tagged commit"

# (4)
read -r pkg_version api_version <<<"$(python3 - <<'PY'
import json
pkg = json.load(open("packages/schemas/ts/package.json", encoding="utf-8"))
api = json.load(open("packages/schemas/jsonschema/openapi.json", encoding="utf-8"))
print(pkg["version"], api["info"]["version"])
PY
)"
if [ "$version" != "$pkg_version" ] || [ "$version" != "$api_version" ]; then
  fail "version mismatch: tag $version, package.json $pkg_version, openapi.json $api_version"
fi
heading="$(grep -E "^## ${version//./\\.}( |$)" packages/schemas/CHANGELOG.md || true)"
[ -n "$heading" ] || fail "CHANGELOG has no '## $version' heading"
case "$heading" in
  *"(unreleased)"*) fail "CHANGELOG heading for $version is still (unreleased)" ;;
esac

# (5)
make -C packages/schemas ts-consumer-check

# (6)
[ -n "${NODE_AUTH_TOKEN:-}" ] || fail "NODE_AUTH_TOKEN is not set"
userconfig="$(mktemp)"
trap 'rm -f "$userconfig"' EXIT
# shellcheck disable=SC2016 # the literal ${NODE_AUTH_TOKEN} is expanded by pnpm, not written out
printf '%s\n' '//npm.pkg.github.com/:_authToken=${NODE_AUTH_TOKEN}' >"$userconfig"
(cd packages/schemas/ts && NPM_CONFIG_USERCONFIG="$userconfig" pnpm publish --no-git-checks)
echo "publish: @maniacultasset/schemas@${version} published"
