"""Meta tests for scripts/ci/contracts_check.py."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path


def test_passes_on_repo_fixtures(repo_root: Path) -> None:
    script = repo_root / "scripts/ci/contracts_check.py"
    assert (
        subprocess.run(
            [sys.executable, str(script), "--fixtures", "packages/schemas/fixtures"],
            cwd=repo_root,
            check=False,
        ).returncode
        == 0
    )


def test_fails_on_missing_fixture(repo_root: Path, tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "CanonicalListing.json").write_text("{}\n")
    out = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts/ci/contracts_check.py"),
            "--fixtures",
            str(fixtures),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0
    assert "missing golden fixture" in out.stderr


def _copy_repo_fixtures(repo_root: Path, dest: Path) -> None:
    shutil.copytree(repo_root / "packages/schemas/fixtures", dest)


def test_fails_on_roundtrip_drift(repo_root: Path, tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    _copy_repo_fixtures(repo_root, fixtures)
    path = fixtures / "ErrorReport.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["__future_unknown_field"] = True
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    out = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts/ci/contracts_check.py"),
            "--fixtures",
            str(fixtures),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0
    assert "round-trip changed: ErrorReport" in out.stderr


def test_fails_on_invalid_fixture(repo_root: Path, tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    _copy_repo_fixtures(repo_root, fixtures)
    path = fixtures / "ErrorReport.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "not_a_valid_status"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    out = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts/ci/contracts_check.py"),
            "--fixtures",
            str(fixtures),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0
    assert "invalid fixture: ErrorReport" in out.stderr


def test_fails_on_orphan_fixture(repo_root: Path, tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    _copy_repo_fixtures(repo_root, fixtures)
    (fixtures / "OrphanModel.json").write_text("{}\n", encoding="utf-8")
    out = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts/ci/contracts_check.py"),
            "--fixtures",
            str(fixtures),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0
    assert "orphan fixture: OrphanModel.json" in out.stderr


def test_fails_on_zero_fixtures(repo_root: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    out = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts/ci/contracts_check.py"),
            "--fixtures",
            str(empty),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0
    assert "no golden fixtures" in out.stderr


def test_imports_only_stdlib_and_mkos_schemas(repo_root: Path) -> None:
    tree = ast.parse((repo_root / "scripts/ci/contracts_check.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in {
                    "mkos_schemas",
                    "argparse",
                    "json",
                    "sys",
                    "pathlib",
                }
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root in {
                "mkos_schemas",
                "pydantic",
                "__future__",
                "argparse",
                "json",
                "sys",
                "pathlib",
            }
