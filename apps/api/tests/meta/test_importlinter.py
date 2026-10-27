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


def test_mfa_contract_present(repo_root: Path) -> None:
    """DPP-1 AC9: only `app.services.mfa` imports the MFA secret unseal."""
    parser = configparser.ConfigParser()
    parser.read(repo_root / ".importlinter", encoding="utf-8")
    mfa = parser["importlinter:contract:mfa-secret-unseal-only-in-mfa-service"]
    assert mfa["type"] == "forbidden"
    assert mfa["forbidden_modules"].split() == ["app.adapters.mfa_secrets"]
    sources = set(mfa["source_modules"].split())
    assert {
        "app.api",
        "app.services",
        "app.repositories",
        "app.cli",
        "app.core",
        "app.db",
        "app.main",
        "app.workers",
        "app.admin",
    } == sources
    assert [line.strip() for line in mfa["ignore_imports"].strip().splitlines()] == [
        "app.services.mfa -> app.adapters.mfa_secrets"
    ]
    assert "allow_indirect_imports" not in mfa


def test_probe_import_from_api_fails(repo_root: Path) -> None:
    """DPP-1 AC9: a router importing the MFA unseal turns `lint-imports` red."""
    import os
    import subprocess

    probe = repo_root / "apps/api/app/api/_probe_mfa.py"
    probe.write_text("import app.adapters.mfa_secrets  # noqa: F401\n", encoding="utf-8")
    try:
        result = subprocess.run(
            ["uv", "run", "--frozen", "lint-imports", "--config", ".importlinter"],
            cwd=repo_root,
            env={**os.environ, "PYTHONPATH": str(repo_root / "apps/api")},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    finally:
        probe.unlink()
    assert result.returncode != 0
    assert "mfa_secrets" in result.stdout + result.stderr
