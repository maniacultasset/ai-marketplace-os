"""The app under RLS (CODE-PLAN S10 Q5, B6; DATABASE.md:129-133): the app engine built from an
`app_rw` DATABASE_URL is neither a superuser nor BYPASSRLS, the seed runs twice through it, and a
repository UPDATE in a session with no tenant GUC finds 0 rows and raises `RowCountError`
instead of writing silently."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from tests.support.dsn import as_app_rw

from . import factories as f
from .conftest import LOCAL_HOSTS, REPO_ROOT

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

Conn = psycopg.Connection[tuple[Any, ...]]


@pytest.fixture
async def app_rw_env(
    db_settings: Any,
    clean_tenant_db: None,
    superuser_conn: Conn,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    """An emptied test database (through the admin login, first); then DATABASE_URL logs in as
    app_rw and ALEMBIC_DATABASE_URL keeps the admin login (the seed's platform rows and purge go
    through it, as migrator)."""
    from app.core.config import get_settings
    from app.db.session import dispose_engine

    admin = os.environ["DATABASE_URL"]
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", admin)
    monkeypatch.setenv("DATABASE_URL", as_app_rw(admin))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    await dispose_engine()
    yield
    await dispose_engine()
    get_settings.cache_clear()


def test_as_app_rw_reads_the_makefile_password() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    url = as_app_rw("postgresql+psycopg://postgres:postgres@localhost:5432/mos_test")
    assert url.startswith("postgresql+psycopg://app_rw:")
    assert url.endswith("@localhost:5432/mos_test")
    password = url.split("://app_rw:", 1)[1].split("@", 1)[0]
    assert f"CI_APP_RW_PASSWORD := {password}\n" in makefile
    with pytest.raises(ValueError, match="scheme://user"):
        as_app_rw("postgresql:///mos_test")


@pytest.mark.usefixtures("app_rw_env")
async def test_app_engine_runs_as_app_rw() -> None:
    from app.db.session import get_engine
    from sqlalchemy import text

    async with get_engine().connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT session_user, current_user, r.rolsuper, r.rolbypassrls "
                    "FROM pg_roles r WHERE r.rolname = current_user"
                )
            )
        ).one()
    assert tuple(row) == ("app_rw", "app_rw", False, False)


@pytest.mark.usefixtures("app_rw_env")
async def test_seed_twice_under_rls(superuser_conn: Conn) -> None:
    """The demo seed writes every tenant row through the app_rw unit of work, under FORCE RLS,
    and a second run is a no-op on the same rows."""
    from app.cli import run_seed

    assert await run_seed("demo", reset=False) == 0
    counts = {
        table: superuser_conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
        for table in ("tenants", "users", "tenant_memberships", "products", "brand_chunks")
    }
    assert counts["tenants"] == (2,)
    assert counts["users"] == (8,)
    assert await run_seed("demo", reset=False) == 0
    again = {
        table: superuser_conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
        for table in counts
    }
    assert again == counts


@pytest.mark.usefixtures("app_rw_env")
async def test_update_one_zero_rows_without_guc(superuser_conn: Conn) -> None:
    """A session that never set `app.tenant_id` (a forgotten unit of work) sees no row, so the
    repository's one-row UPDATE raises instead of returning quietly (DATABASE.md:133)."""
    from app.core.errors import RowCountError
    from app.db.models.catalog import Product
    from app.db.session import get_sessionmaker, unit_of_work
    from app.repositories.base import TenantScopedRepository

    class Products(TenantScopedRepository[Product]):
        model = Product

    tenant = f.tenant(superuser_conn)
    product = f.product(superuser_conn, tenant)
    async with get_sessionmaker()() as session:
        try:
            with pytest.raises(RowCountError):
                await Products(session, tenant).update_one(product, title="no guc")
        finally:
            await session.rollback()
    async with unit_of_work(tenant) as session:
        await Products(session, tenant).update_one(product, title="with guc")
    title = superuser_conn.execute("SELECT title FROM products WHERE id = %s", [product])
    assert title.fetchone() == ("with guc",)
