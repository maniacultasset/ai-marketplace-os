"""AC1, AC2, AC15, AC17: the empty `packages/schemas` skeleton (CANON.md:39-40)."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


def _manifest(repo_root: Path) -> dict[str, object]:
    with (repo_root / "packages" / "schemas" / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_distribution_and_import_names(repo_root: Path) -> None:
    manifest = _manifest(repo_root)
    project = manifest["project"]
    assert isinstance(project, dict)
    assert project["name"] == "mkos-schemas"
    assert project["version"] == "0.0.0"
    wheel = manifest["tool"]["hatch"]["build"]["targets"]["wheel"]  # type: ignore[index]
    assert wheel["packages"] == ["src/mkos_schemas"]
    assert (repo_root / "packages/schemas/src/mkos_schemas/__init__.py").is_file()


def test_no_runtime_dependencies_before_s2(repo_root: Path) -> None:
    project = _manifest(repo_root)["project"]
    assert isinstance(project, dict)
    assert project["dependencies"] == []
    assert project["requires-python"] == "==3.13.*"


def test_py_typed_marker_present(repo_root: Path) -> None:
    assert (repo_root / "packages/schemas/src/mkos_schemas/py.typed").is_file()


def test_import_does_not_pull_app() -> None:
    code = "import sys, mkos_schemas; sys.exit(1 if 'app' in sys.modules else 0)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
