"""Tester re-check attacks on the S5b fix round 1 (AC 14, AC 23; Decision 14): the exact-id
newer-database check and the alembic_version revoke on the failure path. Scratch databases only."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from tests.alembic_helpers import write_revision

from .conftest import ScratchDb

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]
APP_ROLES = ("app_rw", "app_ro", "app_platform_ro")
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
CREATE_PROBE = "CREATE TABLE probe (id bigint PRIMARY KEY)"
DROP_PROBE = "DROP TABLE probe"


def _connect(db: ScratchDb) -> Conn:
    return psycopg.connect(db.libpq, autocommit=True, connect_timeout=5)


def _scalar(db: ScratchDb, query: str, *params: object) -> Any:
    with _connect(db) as conn:
        row = conn.execute(query, params or None).fetchone()
    return None if row is None else row[0]


def _versions(db: ScratchDb) -> list[str]:
    with _connect(db) as conn:
        return [r[0] for r in conn.execute("SELECT version_num FROM alembic_version").fetchall()]


def _assert_locked_down(db: ScratchDb) -> None:
    for role in APP_ROLES:
        for privilege in PRIVILEGES:
            held = _scalar(
                db, "SELECT has_table_privilege(%s, 'alembic_version', %s)", role, privilege
            )
            assert held is False, (role, privilege)
    public = _scalar(
        db,
        "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a "
        "WHERE c.oid = 'alembic_version'::regclass AND a.grantee = 0",
    )
    assert public == 0


def _python_revision(versions: Path, rev: str, down: str | None, raise_stmt: str) -> None:
    """A revision whose upgrade() raises a non-database exception."""
    versions.mkdir(parents=True, exist_ok=True)
    (versions / f"{rev}_scratch.py").write_text(
        f'"""scratch revision {rev}"""\n\n'
        f"revision = {rev!r}\ndown_revision = {down!r}\n"
        "branch_labels = None\ndepends_on = None\n\n\n"
        f"def upgrade() -> None:\n    {raise_stmt}\n\n\n"
        "def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("stored", "reported"),
    [
        ("0001_CORE_TENANCY", "0001_CORE_TENANCY"),
        ("0001_core_tenancy_x", "0001_core_tenancy_x"),
        ("base", "base"),
        ("0001_core_tenancy ", "<invalid revision id>"),
        ("", "<invalid revision id>"),
    ],
)
def test_refuses_near_miss_ids(
    alembic_env: MakeConfig, scratch_db: ScratchDb, tmp_path: Path, stored: str, reported: str
) -> None:
    """Case, suffix, padding, empty and the `base` symbol are all ids this code lacks."""
    from app.db.migrations import DatabaseAheadError

    versions = tmp_path / "versions"
    write_revision(versions, "0001_core_tenancy", [CREATE_PROBE], [DROP_PROBE])
    with _connect(scratch_db) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        conn.execute("INSERT INTO alembic_version VALUES (%s)", (stored,))
        conn.execute("ALTER TABLE alembic_version OWNER TO migrator")
    with pytest.raises(DatabaseAheadError) as info:
        command.upgrade(alembic_env(versions), "head")
    assert info.value.revision == reported
    assert _scalar(scratch_db, "SELECT to_regclass('probe')") is None
    assert _versions(scratch_db) == [stored]


def test_known_mid_chain_revision_still_upgrades(
    alembic_env: MakeConfig, scratch_db: ScratchDb, tmp_path: Path
) -> None:
    """Positive control for the exact-id check: a database at a known, non-head id upgrades."""
    versions = tmp_path / "versions"
    write_revision(versions, "s5a", [CREATE_PROBE], [DROP_PROBE])
    write_revision(versions, "s5b", ["ALTER TABLE probe ADD COLUMN x int"], [], down_revision="s5a")
    cfg = alembic_env(versions)
    command.upgrade(cfg, "s5a")
    command.upgrade(cfg, "head")
    assert _versions(scratch_db) == ["s5b"]
    _assert_locked_down(scratch_db)


@pytest.mark.parametrize(
    ("raise_stmt", "expected"),
    [
        ("raise ValueError('boom')", ValueError),
        ("raise KeyboardInterrupt", KeyboardInterrupt),
    ],
)
def test_non_database_failure_still_revokes(
    alembic_env: MakeConfig,
    scratch_db: ScratchDb,
    tmp_path: Path,
    raise_stmt: str,
    expected: type[BaseException],
) -> None:
    """Revision 1 commits; revision 2 raises a Python error (or is interrupted): the original
    exception surfaces and the app roles still hold nothing on alembic_version."""
    versions = tmp_path / "versions"
    write_revision(versions, "s5a", [CREATE_PROBE], [DROP_PROBE])
    _python_revision(versions, "s5b", "s5a", raise_stmt)
    with pytest.raises(expected):
        command.upgrade(alembic_env(versions), "head")
    assert _versions(scratch_db) == ["s5a"]
    _assert_locked_down(scratch_db)


def test_first_revision_failing_revokes_a_pre_granted_version_table(
    alembic_env: MakeConfig, scratch_db: ScratchDb, tmp_path: Path
) -> None:
    """alembic_version left over with app grants (an older run, or a hand GRANT); the very first
    revision of this run fails, so nothing commits: the revoke still strips the grants."""
    from sqlalchemy.exc import DBAPIError

    versions = tmp_path / "versions"
    write_revision(versions, "s5a", ["SELECT 1/0"], [])
    with _connect(scratch_db) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        conn.execute("ALTER TABLE alembic_version OWNER TO migrator")
        conn.execute("GRANT ALL ON alembic_version TO app_rw, app_ro, app_platform_ro, PUBLIC")
    with pytest.raises(DBAPIError):
        command.upgrade(alembic_env(versions), "head")
    assert _versions(scratch_db) == []
    _assert_locked_down(scratch_db)


def test_cli_failed_revision_exit_1_and_revoked(
    alembic_env: MakeConfig,
    scratch_db: ScratchDb,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import run_migrate

    versions = tmp_path / "versions"
    write_revision(versions, "s5a", [CREATE_PROBE], [DROP_PROBE])
    write_revision(versions, "s5b", ["SELECT 1/0"], [], down_revision="s5a")
    assert run_migrate(alembic_env(versions)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "migrate: failed (DataError, SQLSTATE 22012)\n"
    assert _versions(scratch_db) == ["s5a"]
    _assert_locked_down(scratch_db)
