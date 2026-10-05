"""`python -m app.cli db migrate` on a real database (EXECUTION-PLAN.md:300-305): it applies,
refuses a newer database after taking the lock, times out on a held lock, reports a revision's
own lock timeout as a failure, and never prints a DSN fragment. Scratch databases only."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
AHEAD = "ffffffffffff"
REFUSAL = (
    f"migrate: refused: the database is at revision {AHEAD}, which this image does not contain\n"
)
LEAKS = ("leakuser_s5", "leakpw_s5", "@", "postgresql", "postgres:")
CREATE_PROBE = "CREATE TABLE probe (id bigint PRIMARY KEY)"
DROP_PROBE = "DROP TABLE probe"


def _connect(db: ScratchDb) -> Conn:
    return psycopg.connect(db.libpq, autocommit=True, connect_timeout=5)


def _scalar(db: ScratchDb, query: str, *params: object) -> Any:
    with _connect(db) as conn:
        row = conn.execute(query, params or None).fetchone()
    return None if row is None else row[0]


def _versions(db: ScratchDb) -> list[str]:
    if _scalar(db, "SELECT to_regclass('alembic_version')") is None:
        return []
    with _connect(db) as conn:
        return [r[0] for r in conn.execute("SELECT version_num FROM alembic_version").fetchall()]


def _relations(db: ScratchDb) -> list[str]:
    with _connect(db) as conn:
        rows = conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'S', 'm', 'p') "
            "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass "
            "AND d.objid = c.oid AND d.deptype = 'e') ORDER BY 1"
        ).fetchall()
    return [r[0] for r in rows]


def _seed_ahead(conn: Conn) -> None:
    conn.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
    conn.execute("INSERT INTO alembic_version VALUES (%s)", (AHEAD,))
    conn.execute("ALTER TABLE alembic_version OWNER TO migrator")


@contextmanager
def _holding_migration_lock(db: ScratchDb) -> Iterator[Conn]:
    from app.db.migrations import MIGRATION_LOCK_KEY

    with _connect(db) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        yield conn


def _cli_env(url: str | None) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "APP_ENV": "local", "APP_ROLE": "migrate"}
    if url is not None:
        env["ALEMBIC_DATABASE_URL"] = url
    return env


def _cli(tmp_path: Path, url: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.cli", "db", "migrate"],
        cwd=tmp_path,
        env=_cli_env(url),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _no_leak(*streams: str, extra: tuple[str, ...] = ()) -> None:
    for stream in streams:
        for token in (*LEAKS, *extra):
            assert token not in stream, token


def test_migrate_upgrades_empty_database(
    alembic_env: MakeConfig,
    scratch_db: ScratchDb,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import run_migrate

    versions = tmp_path / "versions"
    write_revision(versions, "s5a", [CREATE_PROBE], [DROP_PROBE])
    assert run_migrate(alembic_env(versions)) == 0
    assert capsys.readouterr().out == "migrate: ok (head s5a)\n"
    assert _relations(scratch_db) == ["alembic_version", "probe"]
    assert _versions(scratch_db) == ["s5a"]


def test_migrate_upgrades_is_noop_at_head(
    alembic_env: MakeConfig,
    scratch_db: ScratchDb,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import run_migrate

    versions = tmp_path / "versions"
    write_revision(versions, "s5a", [CREATE_PROBE], [DROP_PROBE])
    assert run_migrate(alembic_env(versions)) == 0
    before = (_relations(scratch_db), _versions(scratch_db))
    assert run_migrate(alembic_env(versions)) == 0
    assert (_relations(scratch_db), _versions(scratch_db)) == before
    assert capsys.readouterr().out.splitlines()[-1] == "migrate: ok (head s5a)"


def test_migrate_refuses_newer_db(scratch_db: ScratchDb, tmp_path: Path) -> None:
    with _connect(scratch_db) as conn:
        _seed_ahead(conn)
    result = _cli(tmp_path, scratch_db.url)
    assert result.returncode == 3, result.stderr
    assert result.stderr == REFUSAL
    _no_leak(result.stdout)
    assert _relations(scratch_db) == ["alembic_version"]
    assert _versions(scratch_db) == [AHEAD]


def test_migrate_lock_times_out_when_held(
    alembic_env: MakeConfig,
    scratch_db: ScratchDb,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import run_migrate

    versions = tmp_path / "versions"
    write_revision(versions, "s5a", [CREATE_PROBE], [DROP_PROBE])
    cfg = alembic_env(versions)
    with _holding_migration_lock(scratch_db) as holder:
        started = time.monotonic()
        assert run_migrate(cfg) == 4
        elapsed = time.monotonic() - started
        held = holder.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND pid = pg_backend_pid()"
        ).fetchone()
    assert 4.5 <= elapsed <= 10, elapsed
    assert held == (1,)
    assert capsys.readouterr().err == (
        "migrate: another migration holds the lock; nothing was applied\n"
    )
    assert "probe" not in _relations(scratch_db)
    assert _versions(scratch_db) == []


def test_migrate_lock_checks_version_after_lock(scratch_db: ScratchDb, tmp_path: Path) -> None:
    with _holding_migration_lock(scratch_db) as holder:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.cli", "db", "migrate"],
            cwd=tmp_path,
            env=_cli_env(scratch_db.url),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 4
            waiting = 0
            while time.monotonic() < deadline:
                row = holder.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event = 'advisory'"
                ).fetchone()
                waiting = row[0] if row else 0
                if waiting:
                    break
                time.sleep(0.05)
            assert waiting == 1, "the migrate process never waited on the lock"
            _seed_ahead(holder)  # a newer image migrated while this one waited
            holder.execute("SELECT pg_advisory_unlock_all()")
            stdout, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    assert proc.returncode == 3, stderr
    assert stderr == REFUSAL
    _no_leak(stdout)
    assert _versions(scratch_db) == [AHEAD]


def _leaky_url(scratch_db: ScratchDb, host_port: str) -> str:
    return f"postgresql+psycopg://leakuser_s5:leakpw_s5@{host_port}/{scratch_db.name}"


def test_migrate_error_output_bad_credentials(
    alembic_env: MakeConfig,
    scratch_db: ScratchDb,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import run_migrate
    from app.core.config import get_settings

    host_port = scratch_db.url.split("@", 1)[1].split("/", 1)[0]
    cfg = alembic_env(None)
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", _leaky_url(scratch_db, host_port))
    get_settings.cache_clear()
    assert run_migrate(cfg) == 1
    out, err = capsys.readouterr()
    # libpq reports no SQLSTATE for a failed connection (psycopg's `sqlstate` is None), and the
    # message that names 28P01's cause is exactly what must not be printed.
    assert err == "migrate: failed (OperationalError)\n"
    _no_leak(out, err, extra=(host_port, host_port.split(":")[0], scratch_db.name))


def test_migrate_error_output_unreachable_host(
    alembic_env: MakeConfig,
    scratch_db: ScratchDb,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import run_migrate
    from app.core.config import get_settings

    cfg = alembic_env(None)
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", _leaky_url(scratch_db, "127.0.0.1:1"))
    get_settings.cache_clear()
    assert run_migrate(cfg) == 1
    out, err = capsys.readouterr()
    assert err.startswith("migrate: failed (OperationalError")
    _no_leak(out, err, extra=("127.0.0.1", ":1/", scratch_db.name))


def test_migrate_error_output_missing_url(tmp_path: Path) -> None:
    result = _cli(tmp_path, None)
    assert result.returncode != 0
    assert "ALEMBIC_DATABASE_URL" in result.stderr
    _no_leak(result.stdout, result.stderr)
    assert "Traceback" not in result.stderr


def test_migrate_lock_timeout_inside_revision_reports_failure(
    alembic_env: MakeConfig,
    scratch_db: ScratchDb,
    superuser_conn: Conn,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import run_migrate

    versions = tmp_path / "versions"
    write_revision(versions, "s5r1", [CREATE_PROBE], [DROP_PROBE])
    write_revision(
        versions,
        "s5r2",
        ["ALTER TABLE busy ADD COLUMN c int"],
        ["ALTER TABLE busy DROP COLUMN c"],
        down_revision="s5r1",
    )
    with _connect(scratch_db) as setup:
        setup.execute("CREATE TABLE busy (id int)")
        setup.execute("ALTER TABLE busy OWNER TO migrator")
    holder = psycopg.connect(scratch_db.libpq, connect_timeout=5)  # not autocommit
    try:
        holder.execute("LOCK TABLE busy IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        assert run_migrate(alembic_env(versions)) == 1
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()
    assert 4.5 <= elapsed <= 10, elapsed
    assert capsys.readouterr().err == "migrate: failed (OperationalError, SQLSTATE 55P03)\n"
    assert "probe" in _relations(scratch_db)
    assert _versions(scratch_db) == ["s5r1"]
    column = _scalar(
        scratch_db,
        "SELECT count(*) FROM pg_attribute WHERE attrelid = 'busy'::regclass AND attname = 'c'",
    )
    assert column == 0
