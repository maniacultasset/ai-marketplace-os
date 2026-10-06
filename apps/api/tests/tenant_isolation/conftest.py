"""The tenant-isolation suite (CANON.md:137; S9 T15). It runs only through `make tenant-isolation`:
on a migrated `mos_iso`, with DATABASE_URL logging in as `app_rw` and ALEMBIC_DATABASE_URL as the
admin login used for fixtures. Nothing here imports `app` at module scope."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

HERE = Path(__file__).resolve().parent
ISO_DATABASE = "mos_iso"
LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1"]  # psycopg resolves in Python
Conn = psycopg.Connection[tuple[object, ...]]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Every test in this folder is a tenant_isolation test, so `api-tests` deselects it (D8)."""
    for item in items:
        if HERE in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.tenant_isolation)
            item.add_marker(pytest.mark.allow_hosts(LOCAL_HOSTS))


def _url(name: str) -> str:
    url = os.environ.get(name)
    if not url:
        pytest.fail(f"{name} is unset: run this suite through `make tenant-isolation`")
    return url


def libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _checked(conn: Conn) -> Conn:
    row = conn.execute("SELECT current_database()").fetchone()
    if not row or row[0] != ISO_DATABASE:
        conn.close()
        pytest.fail(f"refusing to run the isolation suite against database {row!r}")
    return conn


@pytest.fixture
def admin_conn() -> Iterator[Conn]:
    """Autocommit admin connection (ALEMBIC_DATABASE_URL) to mos_iso, for fixtures only."""
    with _checked(psycopg.connect(libpq(_url("ALEMBIC_DATABASE_URL")), autocommit=True)) as conn:
        yield conn


@pytest.fixture
def app_conn() -> Iterator[Conn]:
    """Autocommit connection as the application login (DATABASE_URL, app_rw)."""
    with _checked(psycopg.connect(libpq(_url("DATABASE_URL")), autocommit=True)) as conn:
        yield conn


@pytest.fixture
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    with _checked(psycopg.connect(libpq(_url("ALEMBIC_DATABASE_URL")))):
        pass
    engine = create_async_engine(_url("ALEMBIC_DATABASE_URL"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    with _checked(psycopg.connect(libpq(_url("DATABASE_URL")))):
        pass
    engine = create_async_engine(_url("DATABASE_URL"))
    yield engine
    await engine.dispose()


# 0004's append-only tables refuse TRUNCATE by trigger, for every role; this harness reset (the
# admin login, test databases only) skips user triggers for its one statement.
CLEAN_SQL = (
    "SET session_replication_role = replica; "
    "TRUNCATE tenants, users, category_templates, rules RESTART IDENTITY CASCADE; "
    "RESET session_replication_role"
)


@pytest.fixture
def clean_tenants(admin_conn: Conn) -> None:
    """Empty every tenant and platform-seed table before the test (schema untouched)."""
    admin_conn.execute(CLEAN_SQL)


@pytest.fixture(scope="module")
def clean_module_admin_conn() -> Iterator[Conn]:
    """A module-wide admin connection on a freshly emptied mos_iso, for a module whose tests
    share one read-only world (they never leave a write behind)."""
    with _checked(psycopg.connect(libpq(_url("ALEMBIC_DATABASE_URL")), autocommit=True)) as conn:
        conn.execute(CLEAN_SQL)
        yield conn


@pytest.fixture
async def iso_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """App settings from the suite's env (DATABASE_URL logs in as app_rw), no developer `.env`,
    and a fresh engine per test (one event loop per test)."""
    from app.core.config import get_settings
    from app.db.session import dispose_engine

    _url("DATABASE_URL")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    await dispose_engine()
    yield
    await dispose_engine()
    get_settings.cache_clear()
