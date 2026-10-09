"""S20 T15 (AC13): the adapter wiring reads accounts through the tenant's RLS session: another
tenant's account id is None through `active_by_id`, under app_rw and FORCE RLS."""

from __future__ import annotations

import pytest

from .conftest import Conn
from .factories import World, build_world


@pytest.fixture(scope="module")
def world(clean_module_admin_conn: Conn) -> World:
    return build_world(clean_module_admin_conn, 2)


async def test_active_by_id_other_tenant_none(iso_settings: None, world: World) -> None:
    from app.repositories.uow import tenant_uow

    a, b = world.tenants
    theirs = world.rows["marketplace_accounts"][b]
    async with tenant_uow(a) as uow:
        assert await uow.marketplace_accounts.active_by_id(theirs) is None
    async with tenant_uow(b) as uow:
        own = await uow.marketplace_accounts.active_by_id(theirs)
        assert own is not None and own.id == theirs
