"""The schema-test harness: `head_db` is a scratch database at the code's head, `db` a rolled-back
transaction on it, and `expect_sqlstate` a savepoint that keeps `db` usable after an error."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic.script import ScriptDirectory

from .conftest import ScratchDb, catalog_snapshot, expect_sqlstate, head_database

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


def test_head_db_at_script_head(head_db: ScratchDb) -> None:
    from app.db.migrations import alembic_config

    heads = set(ScriptDirectory.from_config(alembic_config()).get_heads())
    with psycopg.connect(head_db.libpq, autocommit=True, connect_timeout=5) as conn:
        rows = {r[0] for r in conn.execute("SELECT version_num FROM alembic_version").fetchall()}
    assert rows == heads


def test_expect_sqlstate_leaves_connection_usable(db: Conn) -> None:
    error = expect_sqlstate(db, "22012", "SELECT 1/0")
    assert error.sqlstate == "22012"
    assert db.execute("SELECT 2").fetchone() == (2,)
    with pytest.raises(AssertionError, match="succeeded"):
        expect_sqlstate(db, "22012", "SELECT 1")
    with pytest.raises(AssertionError, match="wanted 23505"):
        expect_sqlstate(db, "23505", "SELECT 1/0")
    assert db.execute("SELECT 3").fetchone() == (3,)


def test_db_rolls_back(head_db: ScratchDb, db: Conn) -> None:
    db.execute("CREATE TABLE harness_probe (id int)")
    db.rollback()
    assert db.execute("SELECT to_regclass('harness_probe')").fetchone() == (None,)


def test_mos_test_untouched(superuser_conn: Conn, tmp_path: Path) -> None:
    """Building and dropping a head database leaves mos_test as it was (at head under
    `make api-tests`, so this compares snapshots rather than assuming it is empty)."""
    before = catalog_snapshot(superuser_conn)
    with head_database(tmp_path) as built:
        assert built.name != superuser_conn.info.dbname
    assert catalog_snapshot(superuser_conn) == before
    gone = superuser_conn.execute(
        "SELECT count(*) FROM pg_database WHERE datname = %s", (built.name,)
    ).fetchone()
    assert gone == (0,)
