#!/usr/bin/env bash
# The contract version check, run by `make contracts` (S3 T22; packages/schemas/CHANGELOG.md rule).
#   (a) packages/schemas/pyproject.toml == ts/package.json == SCHEMA_VERSION (base.py) == every
#       fixture's schema_version, else "schemas: version mismatch";
#   (b) once schemas-v<version> is tagged, the generated contract (jsonschema/, ts/src/) may not
#       change at that version: bump to the next MINOR (or add to an open `(unreleased)` heading).
set -euo pipefail
cd "$(dirname "$0")/../.."

report="$(python3 - <<'PY'
import json
import pathlib
import re

root = pathlib.Path("packages/schemas")
pyproject = (root / "pyproject.toml").read_text("utf-8")
project = re.search(r'^\[project\]\n(?:(?!^\[).*\n)*?^version = "([^"]+)"', pyproject, re.M)
versions = {
    "pyproject.toml": project.group(1) if project else "<missing>",
    "ts/package.json": json.loads((root / "ts/package.json").read_text("utf-8"))["version"],
}
base = (root / "src/mkos_schemas/base.py").read_text("utf-8")
match = re.search(r'^SCHEMA_VERSION: Final = "([^"]+)"', base, re.M)
versions["SCHEMA_VERSION"] = match.group(1) if match else "<missing>"
for fixture in sorted((root / "fixtures").glob("*.json")):
    data = json.loads(fixture.read_text("utf-8"))
    if isinstance(data, dict) and "schema_version" in data:
        versions[f"fixtures/{fixture.name}"] = data["schema_version"]
distinct = sorted(set(versions.values()))
if len(distinct) != 1:
    print("MISMATCH " + ", ".join(f"{k}={v}" for k, v in versions.items()))
else:
    print("OK " + distinct[0])
PY
)"
case "$report" in
  "OK "*) version="${report#OK }" ;;
  *) echo "schemas: version mismatch: ${report#MISMATCH }" >&2; exit 1 ;;
esac

tag="schemas-v${version}"
if git rev-parse --verify --quiet "refs/tags/${tag}" >/dev/null; then
  if ! git diff --quiet "$tag" -- packages/schemas/jsonschema packages/schemas/ts/src; then
    echo "schemas: ${version} already tagged; bump per CHANGELOG rule" >&2
    exit 1
  fi
fi
echo "schemas: version ${version} consistent"
