"""`scripts/db/rls_emergency_disable.sql` (DATABASE.md §11.2 row 6; CODE-PLAN S10 B7): the
migrator-run switch that turns RLS off on every table the catalog lists, with a staff tombstone,
and refuses any other session user. Recovery is `alembic downgrade 0004_integrity` and
`upgrade head`, which puts back the exact 0005 catalog.

psql runs from PATH (CI's runner) or, on a laptop without it, inside this checkout's compose
postgres service (the Makefile exports the compose project)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from .conftest import REPO_ROOT, ScratchDb

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

MakeConfig = Callable[[Path | None], Config]
SCRIPT = REPO_ROOT / "scripts/db/rls_emergency_disable.sql"
OPERATOR = "oncall@staff.test"


def _psql(db: ScratchDb, *, as_migrator: bool, email: str | None = OPERATOR) -> tuple[int, str]:
    options = ["-v", "ON_ERROR_STOP=1", "-X", "-q"]
    if email is not None:
        options += ["-v", f"operator_email={email}"]
    if as_migrator:  # the session user the script checks
        options += ["-c", "SET SESSION AUTHORIZATION migrator"]
    host = shutil.which("psql")
    if host:
        argv = [host, db.libpq, *options, "-f", "-"]
    else:
        argv = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres"]
        argv += ["-d", db.name, *options, "-f", "-"]
    result = subprocess.run(
        argv,
        input=SCRIPT.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def _rls_state(db: ScratchDb) -> dict[str, object]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        flags = conn.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p') ORDER BY 1"
        ).fetchall()
        policies = conn.execute(
            "SELECT tablename, policyname, cmd, roles::text, qual, with_check FROM pg_policies "
            "WHERE schemaname = 'public' ORDER BY 1, 2"
        ).fetchall()
    return {"flags": flags, "policies": policies}


def _tombstones(db: ScratchDb) -> list[tuple[object, ...]]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        return conn.execute(
            "SELECT tenant_id, actor_type, actor_email, entity_type, reason FROM audit_log "
            "WHERE action = 'rls.emergency_disabled'"
        ).fetchall()


def _forced(state: dict[str, object]) -> set[str]:
    flags = state["flags"]
    assert isinstance(flags, list)
    return {str(name) for name, enabled, forced in flags if enabled and forced}


def test_disable_script_refuses_non_migrator(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    command.upgrade(alembic_env(None), "head")
    before = _rls_state(scratch_db)
    assert len(_forced(before)) > 30
    code, output = _psql(scratch_db, as_migrator=False)
    assert code != 0 and "run as migrator" in output, output
    code, output = _psql(scratch_db, as_migrator=True, email=None)
    assert code != 0 and "operator_email" in output, output
    code, output = _psql(scratch_db, as_migrator=True, email="  ")
    assert code != 0 and "operator_email is empty" in output, output
    assert _rls_state(scratch_db) == before
    assert _tombstones(scratch_db) == []


def test_disable_script_disables_all(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    command.upgrade(alembic_env(None), "head")
    forced = _forced(_rls_state(scratch_db))
    code, output = _psql(scratch_db, as_migrator=True)
    assert code == 0, output
    flags = _rls_state(scratch_db)["flags"]
    assert isinstance(flags, list)
    assert [name for name, enabled, force in flags if enabled or force] == []
    assert _tombstones(scratch_db) == [
        (
            None,
            "staff",
            OPERATOR,
            "tenant",
            f"row-level security disabled on {len(forced)} tables",
        )
    ]


def test_recovery_by_reupgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, "head")
    before = _rls_state(scratch_db)
    code, output = _psql(scratch_db, as_migrator=True)
    assert code == 0, output
    assert _rls_state(scratch_db) != before
    command.downgrade(cfg, "0004_integrity")
    command.upgrade(cfg, "head")
    assert _rls_state(scratch_db) == before
