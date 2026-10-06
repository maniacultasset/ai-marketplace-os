"""S9 T6 (AC 11; D2): `platform_scope()` is the one cross-tenant read. Every transaction in it is
READ ONLY and runs as `app_platform_ro` (DATABASE.md:154-158), and it never commits."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import errors as pg_errors
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("db_settings", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]


@pytest.fixture
def two_tenants(superuser_conn: Conn) -> tuple[UUID, UUID]:
    a, b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, a)
    f.product(superuser_conn, b)
    return a, b


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


async def test_platform_scope_reads_both_tenants(two_tenants: tuple[UUID, UUID]) -> None:
    from app.db.models.catalog import Product
    from app.db.session import platform_scope

    async with platform_scope() as session:
        tenants = set((await session.execute(select(Product.tenant_id))).scalars())
    assert tenants == set(two_tenants)


async def test_platform_scope_current_user_is_platform_ro() -> None:
    from app.db.session import platform_scope

    async with platform_scope() as session:
        assert await session.scalar(select(func.current_user())) == "app_platform_ro"
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"


async def test_platform_scope_insert_fails_25006(two_tenants: tuple[UUID, UUID]) -> None:
    from app.db.models.catalog import Product
    from app.db.session import platform_scope

    async with platform_scope() as session:
        with pytest.raises(DBAPIError) as info:
            await session.execute(
                insert(Product).values(
                    tenant_id=two_tenants[0], sku="RO-1", title="t", category="t_shirts"
                )
            )
    assert _sqlstate(info.value) == pg_errors.ReadOnlySqlTransaction.sqlstate


async def test_platform_scope_update_fails_25006(two_tenants: tuple[UUID, UUID]) -> None:
    from app.db.models.catalog import Product
    from app.db.session import platform_scope

    async with platform_scope() as session:
        with pytest.raises(DBAPIError) as info:
            await session.execute(update(Product).values(title="changed"))
    assert _sqlstate(info.value) == pg_errors.ReadOnlySqlTransaction.sqlstate


async def test_platform_scope_hashed_password_42501(superuser_conn: Conn) -> None:
    from app.db.models.tenancy import User
    from app.db.session import platform_scope

    f.user(superuser_conn)
    async with platform_scope() as session:
        with pytest.raises(DBAPIError) as info:
            await session.execute(select(User.hashed_password))
    assert _sqlstate(info.value) == pg_errors.InsufficientPrivilege.sqlstate


async def test_platform_scope_second_transaction_still_read_only() -> None:
    from app.db.session import platform_scope

    async with platform_scope() as session:
        await session.scalar(select(func.now()))
        await session.commit()
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        assert await session.scalar(select(func.current_user())) == "app_platform_ro"


async def test_platform_scope_never_commits(
    superuser_conn: Conn, two_tenants: tuple[UUID, UUID]
) -> None:
    """Even a caller that tries to write gets nothing persisted: the session is rolled back."""
    from app.db.session import platform_scope

    async with platform_scope() as session:
        await session.scalar(select(func.set_config("app.probe", "x", False)))
    row = superuser_conn.execute("SELECT count(*) FROM products").fetchone()
    assert row == (2,)


async def test_tenant_uow_after_platform_scope_has_no_scope_ok(
    two_tenants: tuple[UUID, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With one pooled connection, a tenant session after `platform_scope` is guarded again
    and runs as the login role in a read-write transaction."""
    from app.core.config import get_settings
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import dispose_engine, platform_scope, unit_of_work

    monkeypatch.setenv("DATABASE_POOL_SIZE", "1")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "0")
    get_settings.cache_clear()
    await dispose_engine()
    async with platform_scope() as session:
        pid = await session.scalar(select(func.pg_backend_pid()))
    async with unit_of_work(two_tenants[0]) as session:
        assert await session.scalar(select(func.pg_backend_pid())) == pid
        assert await session.scalar(select(func.current_user())) != "app_platform_ro"
        assert await session.scalar(text("SHOW transaction_read_only")) == "off"
        with pytest.raises(CrossTenantQueryError):
            await session.execute(select(Product))
