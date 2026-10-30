"""AC1: mkos-schemas packaging and import isolation."""

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
    assert project["version"] == "2.1.0"
    wheel = manifest["tool"]["hatch"]["build"]["targets"]["wheel"]  # type: ignore[index]
    assert wheel["packages"] == ["src/mkos_schemas"]
    assert (repo_root / "packages/schemas/src/mkos_schemas/__init__.py").is_file()


def test_runtime_dependency_is_pydantic_only(repo_root: Path) -> None:
    project = _manifest(repo_root)["project"]
    assert isinstance(project, dict)
    assert project["dependencies"] == ["pydantic>=2.11,<3"]
    assert project["requires-python"] == "==3.13.*"


def test_lock_resolves_pydantic_in_range(repo_root: Path) -> None:
    lock_path = repo_root / "uv.lock"
    with lock_path.open("rb") as fh:
        lock = tomllib.load(fh)
    packages = lock.get("package", [])
    pydantic_ver = next(p["version"] for p in packages if p.get("name") == "pydantic")
    major, minor, *_ = pydantic_ver.split(".")
    assert major == "2" and int(minor) >= 11
    mkos = next(p for p in packages if p.get("name") == "mkos-schemas")
    deps = [d.get("name") for d in mkos.get("dependencies", [])]
    assert deps == ["pydantic"]


def test_schema_tests_are_collected(repo_root: Path) -> None:
    with (repo_root / "pyproject.toml").open("rb") as fh:
        root = tomllib.load(fh)
    paths = root["tool"]["pytest"]["ini_options"]["testpaths"]
    assert "packages/schemas/tests" in paths
    tests = list((repo_root / "packages/schemas/tests").glob("test_*.py"))
    assert len(tests) >= 17


def test_import_does_not_pull_app() -> None:
    modules = [
        "agent",
        "auth",
        "base",
        "codes",
        "enums",
        "errors",
        "export",
        "images",
        "imports",
        "jobs",
        "listing",
        "paths",
        "pipeline",
        "publish",
        "rules",
        "validation",
    ]
    code = f"""
import sys
for m in {modules!r}:
    __import__('mkos_schemas.' + m)
forbidden = {{'app', 'sqlalchemy', 'fastapi', 'starlette', 'psycopg'}}
bad = forbidden & set(sys.modules)
sys.exit(1 if bad else 0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_py_typed_marker_present(repo_root: Path) -> None:
    assert (repo_root / "packages/schemas/src/mkos_schemas/py.typed").is_file()
