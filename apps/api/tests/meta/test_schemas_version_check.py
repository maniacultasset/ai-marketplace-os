"""S3 T22: scripts/ci/schemas_version_check.sh (the post-tag version rule, run by `make contracts`).

Temp git repos holding the script and the version-bearing files; no network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path("scripts/ci/schemas_version_check.sh")
IDENT = [
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.invalid",
    "-c",
    "core.hooksPath=/dev/null",
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *IDENT, *args], cwd=repo, check=True, capture_output=True)


def _set_version(repo: Path, version: str, package_version: str | None = None) -> None:
    root = repo / "packages/schemas"
    (root / "src/mkos_schemas").mkdir(parents=True, exist_ok=True)
    (root / "ts/src").mkdir(parents=True, exist_ok=True)
    (root / "fixtures").mkdir(parents=True, exist_ok=True)
    (root / "jsonschema").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "mkos-schemas"\nversion = "{version}"\n', "utf-8"
    )
    (root / "ts/package.json").write_text(
        json.dumps({"version": package_version or version}), "utf-8"
    )
    (root / "src/mkos_schemas/base.py").write_text(
        f'from typing import Final\n\nSCHEMA_VERSION: Final = "{version}"\n', "utf-8"
    )
    (root / "fixtures/A.json").write_text(json.dumps({"schema_version": version}), "utf-8")
    (root / "fixtures/B.json").write_text(json.dumps({"no_version": True}), "utf-8")


@pytest.fixture
def repo(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts/ci").mkdir(parents=True)
    shutil.copy(repo_root / SCRIPT, root / SCRIPT)
    _git(root, "init", "-q")
    _set_version(root, "1.0.0")
    (root / "packages/schemas/jsonschema/openapi.json").write_text('{"a": 1}\n', "utf-8")
    (root / "packages/schemas/ts/src/api.d.ts").write_text("export type A = 1;\n", "utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=repo, capture_output=True, text=True, check=False
    )


def test_version_check_no_tag_passes(repo: Path) -> None:
    result = _run(repo)
    assert result.returncode == 0, result.stderr
    assert "schemas: version 1.0.0 consistent" in result.stdout


def test_version_check_tag_unchanged_passes(repo: Path) -> None:
    _git(repo, "tag", "schemas-v1.0.0")
    (repo / "README.md").write_text("unrelated\n", "utf-8")
    assert _run(repo).returncode == 0


def test_version_check_tag_changed_fails(repo: Path) -> None:
    _git(repo, "tag", "schemas-v1.0.0")
    (repo / "packages/schemas/jsonschema/openapi.json").write_text('{"a": 2}\n', "utf-8")
    result = _run(repo)
    assert result.returncode == 1
    assert "schemas: 1.0.0 already tagged; bump per CHANGELOG rule" in result.stderr


def test_version_check_ts_change_after_tag_fails(repo: Path) -> None:
    _git(repo, "tag", "schemas-v1.0.0")
    (repo / "packages/schemas/ts/src/api.d.ts").write_text("export type A = 2;\n", "utf-8")
    assert _run(repo).returncode == 1


def test_version_check_bumped_passes(repo: Path) -> None:
    _git(repo, "tag", "schemas-v1.0.0")
    (repo / "packages/schemas/jsonschema/openapi.json").write_text('{"a": 2}\n', "utf-8")
    _set_version(repo, "1.1.0")
    result = _run(repo)
    assert result.returncode == 0, result.stderr
    assert "version 1.1.0 consistent" in result.stdout


def test_version_check_mismatch_fails(repo: Path) -> None:
    _set_version(repo, "1.0.0", package_version="1.1.0")
    result = _run(repo)
    assert result.returncode == 1
    assert "schemas: version mismatch" in result.stderr
    assert "ts/package.json=1.1.0" in result.stderr


def test_version_check_fixture_mismatch_fails(repo: Path) -> None:
    (repo / "packages/schemas/fixtures/A.json").write_text('{"schema_version": "0.9.0"}', "utf-8")
    result = _run(repo)
    assert result.returncode == 1
    assert "fixtures/A.json=0.9.0" in result.stderr


def test_contracts_runs_the_version_check(repo_root: Path) -> None:
    text = (repo_root / "scripts/ci/contracts.sh").read_text("utf-8")
    assert text.index("scripts/ci/schemas_version_check.sh") < text.index(
        "scripts/ci/oasdiff_check.sh"
    )


def test_repo_versions_consistent(repo_root: Path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)], cwd=repo_root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
