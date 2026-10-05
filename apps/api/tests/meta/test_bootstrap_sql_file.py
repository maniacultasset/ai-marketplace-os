"""The bootstrap file stays free of password literals and orders the Cloud SQL grant."""

from __future__ import annotations

import re
from pathlib import Path


def _bootstrap(repo_root: Path) -> str:
    return (repo_root / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")


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


def _statements(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("--")]


def test_default_privileges_run_as_migrator(repo_root: Path) -> None:
    """A non-superuser holds migrator WITH INHERIT FALSE (DATABASE.md:1752), so the schema grant
    and the default privileges run under SET ROLE migrator."""
    text = "\n".join(_statements(_bootstrap(repo_root)))
    owner = text.index("ALTER SCHEMA public OWNER TO migrator")
    set_role = text.index("SET ROLE migrator;")
    usage = text.index("GRANT USAGE ON SCHEMA public TO")
    reset = text.index("RESET ROLE;")
    defaults = [m.start() for m in re.finditer(r"ALTER DEFAULT PRIVILEGES", text)]
    assert len(defaults) == 3
    assert owner < set_role < usage < min(defaults)
    assert max(defaults) < reset
    assert text.count("SET ROLE migrator;") == 1
    assert text.count("RESET ROLE;") == 1


def test_execute_revoke_is_global(repo_root: Path) -> None:
    """The schema-scoped form leaves PUBLIC EXECUTE in place (DATABASE.md:1752)."""
    text = " ".join(" ".join(_statements(_bootstrap(repo_root))).split())
    statements = [s.strip() for s in text.split(";")]
    revoke = [s for s in statements if "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC" in s]
    assert revoke == [
        "ALTER DEFAULT PRIVILEGES FOR ROLE migrator REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    ]
