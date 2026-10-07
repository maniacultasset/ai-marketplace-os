"""S14b T12 (audit `no-version-bump-after-tag`): every version field of the contract agrees, and
the CHANGELOG's top heading names that version, so a contract change can never ship without its
version step (CHANGELOG "Version rule after a tag")."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def _schema_versions(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "schema_version" and isinstance(value, str):
                yield value
            else:
                yield from _schema_versions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _schema_versions(item)


def _version(repo_root: Path) -> str:
    with (repo_root / "packages/schemas/pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def test_versions_agree(repo_root: Path) -> None:
    from mkos_schemas.base import SCHEMA_VERSION

    version = _version(repo_root)
    package = json.loads((repo_root / "packages/schemas/ts/package.json").read_text("utf-8"))
    spec = json.loads((repo_root / "packages/schemas/jsonschema/openapi.json").read_text("utf-8"))
    assert package["version"] == version
    assert version == SCHEMA_VERSION
    assert spec["info"]["version"] == version
    fixtures = sorted((repo_root / "packages/schemas/fixtures").glob("*.json"))
    assert fixtures
    for path in fixtures:
        found = set(_schema_versions(json.loads(path.read_text("utf-8"))))
        assert found <= {version}, (path.name, found)


def test_changelog_top_heading_matches_version(repo_root: Path) -> None:
    text = (repo_root / "packages/schemas/CHANGELOG.md").read_text("utf-8")
    heading = next(line for line in text.splitlines() if line.startswith("## "))
    match = re.fullmatch(r"## (\d+\.\d+\.\d+)(?: \((?:unreleased|\d{4}-\d{2}-\d{2})\))?", heading)
    assert match, heading
    assert match.group(1) == _version(repo_root)
