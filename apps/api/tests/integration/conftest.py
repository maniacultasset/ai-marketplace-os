"""Integration fixtures: a real Postgres, and never the dev database `marketplace_os`."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql

if TYPE_CHECKING:
    from alembic.config import Config
    from app.core.config import Settings

ALLOWED_DATABASES = frozenset({"mos_test", "mos_iso"})


def libpq_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
async def db_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Settings]:
    """Settings for the test database. Fails (never skips) unless DATABASE_URL points at
    mos_test or mos_iso; disposes the engine after each test (one event loop per test)."""
    from app.core.config import get_settings
    from app.db.session import dispose_engine

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail("DATABASE_URL is unset: run the integration tests through `make api-tests`")
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
    database = row[0] if row else None
    if database not in ALLOWED_DATABASES:
        pytest.fail(f"refusing to run integration tests against database {database!r}")
    monkeypatch.chdir(tmp_path)  # a developer's .env must not leak in
    get_settings.cache_clear()
    await dispose_engine()
    yield get_settings()
    await dispose_engine()
    get_settings.cache_clear()


@pytest.fixture
def superuser_conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """An autocommit connection to the test database (mos_test or mos_iso only)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail("DATABASE_URL is unset: run the integration tests through `make api-tests`")
    with psycopg.connect(libpq_url(url), autocommit=True, connect_timeout=5) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
        if not row or row[0] not in ALLOWED_DATABASES:
            pytest.fail(f"refusing to run integration tests against database {row!r}")
        yield conn


# ---- Alembic: a scratch database per test, so mos_test never holds a table or alembic_version ----

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRATCH_PREFIX = "mos_scratch_"


@dataclass(frozen=True)
class ScratchDb:
    name: str
    libpq: str  # postgresql://… for psycopg
    url: str  # postgresql+psycopg://… for SQLAlchemy and ALEMBIC_DATABASE_URL


@contextmanager
def scratch_database(admin: psycopg.Connection[tuple[object, ...]]) -> Iterator[ScratchDb]:
    """CREATE DATABASE mos_scratch_<hex> next to the test database, run 0000 in it, and drop it
    (WITH FORCE) afterwards, whatever the test did."""
    base_url = os.environ["DATABASE_URL"]
    name = f"{SCRATCH_PREFIX}{secrets.token_hex(4)}"
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        url = base_url.rsplit("/", 1)[0] + "/" + name
        bootstrap = (REPO_ROOT / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")
        with psycopg.connect(libpq_url(url), autocommit=True, connect_timeout=5) as conn:
            conn.execute(bootstrap)  # a committed file, not user input
        yield ScratchDb(name=name, libpq=libpq_url(url), url=url)
    finally:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def assert_no_scratch_revision_in(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """`make api-tests` migrates mos_test to the code's head before pytest; a test must never
    stamp anything else there (its revisions belong in a scratch database)."""
    from alembic.script import ScriptDirectory
    from app.db.migrations import alembic_config

    if conn.execute("SELECT to_regclass('alembic_version')").fetchone() == (None,):
        return
    heads = set(ScriptDirectory.from_config(alembic_config()).get_heads())
    rows = {str(r[0]) for r in conn.execute("SELECT version_num FROM alembic_version").fetchall()}
    assert rows <= heads, f"mos_test holds revisions {sorted(rows - heads)}"


@pytest.fixture
def scratch_db(superuser_conn: psycopg.Connection[tuple[object, ...]]) -> Iterator[ScratchDb]:
    with scratch_database(superuser_conn) as db:
        yield db


@pytest.fixture
def alembic_env(
    scratch_db: ScratchDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[Path | None], Config]]:
    """Settings that point ALEMBIC_DATABASE_URL at the scratch database (no `.env` leaks in),
    and a factory for the repo's Alembic config, optionally with a scratch `versions/`."""
    from app.core.config import get_settings
    from app.db.migrations import alembic_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", scratch_db.url)
    get_settings.cache_clear()

    def make_config(versions: Path | None = None) -> Config:
        cfg = alembic_config()
        if versions is not None:
            versions.mkdir(parents=True, exist_ok=True)
            cfg.set_main_option("version_locations", str(versions))
        return cfg

    yield make_config
    get_settings.cache_clear()


# ---- 0001 onward: one scratch database at the Alembic head per session ----
#
# `make api-tests` leaves mos_test at the code's head before pytest, and tests leave it
# unchanged: schema tests write only to `head_db` (or a per-test `scratch_db`).

Conn = psycopg.Connection[tuple[object, ...]]


def catalog_snapshot(conn: Conn) -> dict[str, list[tuple[object, ...]]]:
    """Relations and functions in `public` that are not extension members, plus the recorded
    Alembic revisions: what a test must leave unchanged in mos_test."""
    relations = conn.execute(
        "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT EXISTS (SELECT 1 FROM pg_depend d "
        "WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e') "
        "ORDER BY 1, 2"
    ).fetchall()
    functions = conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass "
        "AND d.objid = p.oid AND d.deptype = 'e') ORDER BY 1"
    ).fetchall()
    versions: list[tuple[object, ...]] = []
    if conn.execute("SELECT to_regclass('alembic_version')").fetchone() != (None,):
        versions = conn.execute("SELECT version_num FROM alembic_version ORDER BY 1").fetchall()
    return {"relations": relations, "functions": functions, "versions": versions}


def _admin_connect() -> Conn:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail("DATABASE_URL is unset: run the integration tests through `make api-tests`")
    conn = psycopg.connect(libpq_url(url), autocommit=True, connect_timeout=5)
    row = conn.execute("SELECT current_database()").fetchone()
    if not row or row[0] not in ALLOWED_DATABASES:
        conn.close()
        pytest.fail(f"refusing to run integration tests against database {row!r}")
    return conn


def upgrade_to_head(db: ScratchDb, tmp: Path) -> None:
    """`alembic upgrade head` on a scratch database, through env.py (as migrator), with no
    developer `.env` and a settings cache that is cold before and after."""
    from alembic import command
    from app.core.config import get_settings
    from app.db.migrations import alembic_config

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp)
        patch.setenv("APP_ENV", "local")
        patch.setenv("ALEMBIC_DATABASE_URL", db.url)
        get_settings.cache_clear()
        try:
            command.upgrade(alembic_config(), "head")
        finally:
            get_settings.cache_clear()


@contextmanager
def head_database(tmp: Path) -> Iterator[ScratchDb]:
    """A scratch database with 0000 applied and the code's Alembic head, dropped afterwards."""
    with _admin_connect() as admin, scratch_database(admin) as db:
        upgrade_to_head(db, tmp)
        yield db


@pytest.fixture(scope="session")
def head_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScratchDb]:
    with head_database(tmp_path_factory.mktemp("head_db")) as db:
        yield db


@pytest.fixture
def db(head_db: ScratchDb) -> Iterator[Conn]:
    """A connection to `head_db` as `postgres`, inside one transaction that is rolled back."""
    with psycopg.connect(head_db.libpq, connect_timeout=5) as conn:
        try:
            yield conn
        finally:
            conn.rollback()


def expect_sqlstate(
    conn: Conn,
    code: str,
    query: str | sql.SQL | sql.Composed,
    params: Sequence[Any] | Mapping[str, Any] | None = None,
    *,
    constraint: str | None = None,
) -> psycopg.Error:
    """Run one statement inside a savepoint and assert it fails with `code` (and, when given,
    on the named constraint). The connection stays usable afterwards."""
    try:
        with conn.transaction():
            conn.execute(query, params)
    except psycopg.Error as error:
        assert error.sqlstate == code, f"wanted {code}, got {error.sqlstate}: {error}"
        if constraint is not None:
            assert error.diag.constraint_name == constraint, error.diag.constraint_name
        return error
    raise AssertionError(f"wanted SQLSTATE {code}, the statement succeeded")


# ---- S9: tests that commit tenant rows into mos_test ----

LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1"]


@pytest.fixture
def clean_tenant_db(superuser_conn: Conn) -> None:
    """Empty every tenant table (through the `tenants`/`users` cascade) and the platform seed
    tables before the test. Schema objects are untouched, so `alembic check` stays clean."""
    superuser_conn.execute(
        "TRUNCATE tenants, users, category_templates, rules RESTART IDENTITY CASCADE"
    )
