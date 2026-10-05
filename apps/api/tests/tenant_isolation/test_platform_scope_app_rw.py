"""S9 T19 (AC 11): `platform_scope()` as `app_rw` switches to `app_platform_ro`, reads every
tenant, cannot write (25006) and cannot read `users.hashed_password` (42501)."""

from __future__ import annotations

from uuid import UUID

import psycopg
import pytest
from psycopg import errors as pg_errors
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from .factories import World, build_world

pytestmark = pytest.mark.usefixtures("iso_settings")

Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture(scope="module")
def world(clean_module_admin_conn: Conn) -> World:
    return build_world(clean_module_admin_conn, 2)


async def test_platform_scope_as_app_rw_reads_both(world: World) -> None:
    from app.db.models.catalog import Product
    from app.db.session import platform_scope

    async with platform_scope() as session:
        assert await session.scalar(select(func.session_user())) == "app_rw"
        assert await session.scalar(select(func.current_user())) == "app_platform_ro"
        tenants: set[UUID] = set((await session.execute(select(Product.tenant_id))).scalars())
    assert tenants == set(world.tenants)


async def test_platform_scope_as_app_rw_write_25006(world: World) -> None:
    from app.db.models.catalog import Product
    from app.db.session import platform_scope

    async with platform_scope() as session:
        with pytest.raises(DBAPIError) as info:
            await session.execute(update(Product).values(title="x"))
    assert getattr(info.value.orig, "sqlstate", None) == pg_errors.ReadOnlySqlTransaction.sqlstate


async def test_platform_scope_hashed_password_42501(world: World) -> None:
    from app.db.models.tenancy import User
    from app.db.session import platform_scope

    async with platform_scope() as session:
        with pytest.raises(DBAPIError) as info:
            await session.execute(select(User.hashed_password))
    assert getattr(info.value.orig, "sqlstate", None) == pg_errors.InsufficientPrivilege.sqlstate
