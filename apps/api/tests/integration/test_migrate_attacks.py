"""Tester attacks on `db migrate` and alembic/env.py (S5b, AC 13-16, 22): two migrations racing,
the lock after a failure, session state across per-migration commits, and a database holding one
known and one unknown head. Scratch databases only."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic.config import Config
from tests.alembic_helpers import write_revision

from .conftest import ScratchDb

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]
API_ROOT = Path(__file__).resolve().parents[2]

# Runs `python -m app.cli db migrate` exactly, except that versions/ is the scratch directory.
DRIVER = """
import sys
import app.cli as cli
from app.db.migrations import alembic_config

def _config():
    cfg = alembic_config()
    cfg.set_main_option("version_locations", sys.argv[1])
    return cfg

cli.alembic_config = _config
raise SystemExit(cli.main(["db", "migrate"]))
"""

SESSION_STATE_OK = (
    "DO $$ BEGIN IF current_user <> 'migrator' OR current_setting('lock_timeout') <> '5s' "
    "OR (SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
    "AND pid = pg_backend_pid()) <> 1 THEN RAISE EXCEPTION 'session state lost'; END IF; END $$"
)


def _connect(db: ScratchDb) -> Conn:
    return psycopg.connect(db.libpq, autocommit=True, connect_timeout=5)


def _rows(db: ScratchDb, query: str) -> list[tuple[Any, ...]]:
    with _connect(db) as conn:
        return conn.execute(query).fetchall()


def _spawn(tmp_path: Path, db: ScratchDb, versions: Path) -> subprocess.Popen[str]:
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(API_ROOT),
        "APP_ENV": "local",
        "APP_ROLE": "migrate",
        "ALEMBIC_DATABASE_URL": db.url,
    }
    return subprocess.Popen(
        [sys.executable, str(driver), str(versions)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_concurrent_migrates_apply_once(scratch_db: ScratchDb, tmp_path: Path) -> None:
    """Two deploys start together: one applies, the other waits on the lock, then sees the head
    and applies nothing. Neither fails, and the revision ran exactly once."""
    versions = tmp_path / "versions"
    write_revision(
        versions,
        "race1",
        [
            "CREATE TABLE race_probe (id bigint)",
            "INSERT INTO race_probe VALUES (1)",
            "SELECT pg_sleep(2)",
        ],
        ["DROP TABLE race_probe"],
    )
    started = time.monotonic()
    procs = [_spawn(tmp_path, scratch_db, versions) for _ in range(2)]
    results = []
    try:
        for proc in procs:
            out, err = proc.communicate(timeout=30)
            results.append((proc.returncode, out, err))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    elapsed = time.monotonic() - started
    for code, out, err in results:
        assert code == 0, err
        assert err == ""
        assert out.splitlines()[-1] == "migrate: ok (head race1)"
    assert _rows(scratch_db, "SELECT count(*) FROM race_probe") == [(1,)]
    assert _rows(scratch_db, "SELECT version_num FROM alembic_version") == [("race1",)]
    assert elapsed < 5 + 15, elapsed


def test_lock_released_after_failed_migration(
    alembic_env: MakeConfig,
    scratch_db: ScratchDb,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed revision leaves no advisory lock behind, so the fixed image can migrate at once."""
    from app.cli import run_migrate
    from app.db.migrations import MIGRATION_LOCK_KEY

    versions = tmp_path / "versions"
    bad = write_revision(versions, "fail1", ["SELECT 1/0"], [])
    assert run_migrate(alembic_env(versions)) == 1
    assert capsys.readouterr().err == "migrate: failed (DataError, SQLSTATE 22012)\n"
    with _connect(scratch_db) as conn:
        assert conn.execute(
            "SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK_KEY,)
        ).fetchone() == (True,)
        conn.execute("SELECT pg_advisory_unlock_all()")
    bad.unlink()
    write_revision(versions, "fail1", ["CREATE TABLE fixed_probe (id bigint)"], [])
    assert run_migrate(alembic_env(versions)) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "migrate: ok (head fail1)"


def test_session_state_survives_per_migration_commits(
    alembic_env: MakeConfig, scratch_db: ScratchDb, tmp_path: Path
) -> None:
    """`transaction_per_migration` commits after each revision; the role switch, the lock and the
    5 s lock_timeout are session state, so the third revision still runs with all three."""
    from app.cli import run_migrate

    versions = tmp_path / "versions"
    write_revision(versions, "st1", [SESSION_STATE_OK, "CREATE TABLE st_one (id bigint)"], [])
    write_revision(versions, "st2", [SESSION_STATE_OK], [], down_revision="st1")
    write_revision(
        versions,
        "st3",
        [SESSION_STATE_OK, "CREATE TABLE st_three (id bigint)"],
        [],
        down_revision="st2",
    )
    assert run_migrate(alembic_env(versions)) == 0
    owners = _rows(
        scratch_db,
        "SELECT tablename, tableowner FROM pg_tables WHERE tablename LIKE 'st_%' ORDER BY 1",
    )
    assert owners == [("st_one", "migrator"), ("st_three", "migrator")]


def test_refuses_when_one_of_two_heads_is_unknown(
    alembic_env: MakeConfig,
    scratch_db: ScratchDb,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import run_migrate

    versions = tmp_path / "versions"
    write_revision(versions, "known1", ["CREATE TABLE known_probe (id bigint)"], [])
    with _connect(scratch_db) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        conn.execute("INSERT INTO alembic_version VALUES ('known1'), ('ffffffffffff')")
        conn.execute("ALTER TABLE alembic_version OWNER TO migrator")
    assert run_migrate(alembic_env(versions)) == 3
    assert capsys.readouterr().err == (
        "migrate: refused: the database is at revision ffffffffffff, "
        "which this image does not contain\n"
    )
    assert _rows(scratch_db, "SELECT to_regclass('known_probe')") == [(None,)]
