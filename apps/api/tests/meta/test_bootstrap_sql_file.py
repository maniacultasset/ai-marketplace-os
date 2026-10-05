"""The bootstrap file stays free of password literals and orders the Cloud SQL grant."""

from __future__ import annotations

from pathlib import Path


def test_bootstrap_sql_has_no_password_literal(repo_root: Path) -> None:
    text = (repo_root / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")
    assert "PASSWORD" not in text
    assert ":'" not in text


def test_grant_migrator_precedes_owner_change(repo_root: Path) -> None:
    text = (repo_root / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")
    grant = text.index("GRANT migrator TO")
    owner = text.index("ALTER SCHEMA public OWNER TO migrator")
    assert grant < owner


def test_workflows_have_no_password_literals(repo_root: Path) -> None:
    text = (repo_root / ".github/workflows/ci-api.yml").read_text(encoding="utf-8")
    assert "PASSWORD '" not in text
    assert 'PASSWORD "' not in text
