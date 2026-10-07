"""S14b T17 (D13; CODE-PLAN §6 row): the admin's dependency set is exactly the approved pins."""

from __future__ import annotations

import tomllib
from pathlib import Path

APPROVED = {
    "sqladmin": "0.32.0",
    "wtforms": "3.2.2",
    "jinja2": "3.1.6",
    "python-multipart": "0.0.32",
    "markupsafe": "3.0.3",
}


def _packages(repo_root: Path) -> dict[str, dict[str, object]]:
    with (repo_root / "uv.lock").open("rb") as fh:
        return {pkg["name"]: pkg for pkg in tomllib.load(fh)["package"]}


def test_sqladmin_lock_exact(repo_root: Path) -> None:
    packages = _packages(repo_root)
    assert {name: packages[name]["version"] for name in APPROVED} == APPROVED
    sqladmin = packages["sqladmin"]
    deps = {d["name"] for d in sqladmin["dependencies"]}  # type: ignore[attr-defined]
    assert deps <= {"jinja2", "python-multipart", "sqlalchemy", "starlette", "wtforms"}, deps
    assert "itsdangerous" not in packages  # no sqladmin[auth] (D13)


def test_api_declares_sqladmin_without_extras(repo_root: Path) -> None:
    with (repo_root / "apps/api/pyproject.toml").open("rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    assert "sqladmin==0.32.0" in deps
    assert not [d for d in deps if d.startswith("sqladmin[")]
