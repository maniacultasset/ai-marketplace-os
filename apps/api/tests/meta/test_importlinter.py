"""S14b T2 (AC 4): the pipeline import contracts exist in `.importlinter`."""

from __future__ import annotations

import configparser
from pathlib import Path


def test_pipeline_contracts_present(repo_root: Path) -> None:
    parser = configparser.ConfigParser()
    parser.read(repo_root / ".importlinter", encoding="utf-8")
    roots = parser["importlinter"]["root_packages"].split()
    assert "mkos_pipeline" in roots
    pure = parser["importlinter:contract:pipeline-never-imports-app"]
    assert pure["type"] == "forbidden"
    assert pure["source_modules"].split() == ["mkos_pipeline"]
    assert {"app", "sqlalchemy", "fastapi", "starlette", "psycopg", "procrastinate"} <= set(
        pure["forbidden_modules"].split()
    )
    only = parser["importlinter:contract:only-workers-import-pipeline"]
    assert only["forbidden_modules"].split() == ["mkos_pipeline"]
    sources = set(only["source_modules"].split())
    assert {"app.api", "app.services", "app.repositories", "app.adapters", "app.core"} <= sources
    assert {"app.db", "app.main", "app.cli"} <= sources
    assert "app.workers" not in sources


def test_admin_contract_present(repo_root: Path) -> None:
    parser = configparser.ConfigParser()
    parser.read(repo_root / ".importlinter", encoding="utf-8")
    admin = parser["importlinter:contract:only-main-imports-admin"]
    assert admin["forbidden_modules"].split() == ["app.admin"]
    sources = set(admin["source_modules"].split())
    assert "app.main" not in sources
    assert {"app.api", "app.services", "app.repositories", "app.workers", "app.core"} <= sources
    assert (
        "app.admin"
        in parser["importlinter:contract:only-workers-import-pipeline"]["source_modules"].split()
    )
