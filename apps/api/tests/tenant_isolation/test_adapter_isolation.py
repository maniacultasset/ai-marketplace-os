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


async def test_open_adapter_flipkart_other_tenant_not_found(
    iso_settings: None, world: World, clean_module_admin_conn: Conn
) -> None:
    """S22 T13: bharat never reads acme's Flipkart account through `open_adapter` (RLS), and
    the refusal comes before any flag, endpoint or credential step."""
    import uuid

    from app.workers.adapters import AccountNotFound, open_adapter

    a, b = world.tenants
    theirs = world.rows["marketplace_accounts"][b]
    flipkart_id = uuid.uuid4()
    clean_module_admin_conn.execute(
        "INSERT INTO marketplace_accounts SELECT (jsonb_populate_record(NULL::marketplace_accounts,"
        " to_jsonb(m) || jsonb_build_object('id', %s::text, 'marketplace', 'flipkart',"
        " 'seller_id', 'FKISO1'))).* FROM marketplace_accounts m WHERE m.id = %s",
        [str(flipkart_id), theirs],
    )
    try:
        with pytest.raises(AccountNotFound):
            await open_adapter(a, flipkart_id)
        from app.repositories.uow import tenant_uow

        async with tenant_uow(b) as uow:
            own = await uow.marketplace_accounts.active_by_id(flipkart_id)
            assert own is not None and own.marketplace == "flipkart"
    finally:
        clean_module_admin_conn.execute(
            "DELETE FROM marketplace_accounts WHERE id = %s", [flipkart_id]
        )


async def test_preview_source_colours_other_tenant_invisible(
    iso_settings: None, world: World
) -> None:
    """S22 T7: the colourway, cell and image reads behind `preview_source` run in the caller's
    RLS session: another tenant's listing is None, its colours never leak."""
    from app.repositories.uow import tenant_uow

    a, b = world.tenants
    theirs = world.rows["listings"][b]
    async with tenant_uow(a) as uow:
        assert await uow.listings.preview_source(theirs) is None
