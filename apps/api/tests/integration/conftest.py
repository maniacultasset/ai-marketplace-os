"""Integration fixtures: a real Postgres, and never the dev database `marketplace_os`."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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
