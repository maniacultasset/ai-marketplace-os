"""The S13b import repositories under app_rw and FORCE RLS (CODE-PLAN S13 §7 tenant-isolation):
another tenant's import, its rows and its products read as missing, its live imports are not
counted, and the import writers refuse a session of another tenant."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import Conn
from .factories import World, build_world


@pytest.fixture(scope="module")
def world(clean_module_admin_conn: Conn) -> World:
    return build_world(clean_module_admin_conn, 2)


async def test_import_reads_are_tenant_scoped(iso_settings: None, world: World) -> None:
    from app.db.session import unit_of_work
    from app.repositories.imports import ImportRepository, ImportRowRepository

    a, b = world.tenants
    theirs = world.rows["imports"][b]
    async with unit_of_work(a) as session:
        assert await ImportRepository(session, a).get(theirs) is None
        rows = ImportRowRepository(session, a)
        assert (await rows.totals(theirs)).total == 0
        assert await rows.finished_row_numbers(theirs) == set()
        assert await rows.failed_rows(theirs) == []
        assert await rows.page(theirs, status=None, after=None, limit=200) == ([], False)
    async with unit_of_work(b) as session:
        assert await ImportRepository(session, b).get(theirs) is not None
        own_row = world.rows["import_rows"][b]
        assert await ImportRowRepository(session, b).row_number_of(theirs, own_row) is not None
    async with unit_of_work(a) as session:
        assert await ImportRowRepository(session, a).row_number_of(theirs, own_row) is None


async def test_import_live_count_is_tenant_scoped(
    iso_settings: None, world: World, admin_conn: Conn
) -> None:
    from app.db.session import unit_of_work
    from app.repositories.imports import ImportRepository

    a, b = world.tenants
    before: dict[Any, int] = {}
    for tenant in (a, b):
        async with unit_of_work(tenant) as session:
            before[tenant] = await ImportRepository(session, tenant).count_live_bulk_imports()
    row = admin_conn.execute(
        "SELECT count(*) FROM job_runs WHERE tenant_id = %s AND kind = 'bulk_import' "
        "AND status IN ('queued', 'running')",
        [a],
    ).fetchone()
    assert row is not None and before[a] == row[0]


async def test_import_writers_refuse_another_tenant_session(
    iso_settings: None, world: World
) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.session import unit_of_work
    from app.repositories.imports import ImportRepository, ImportRowRepository, RowWrite
    from app.repositories.products import ProductRepository, ProductUpsert

    a, b = world.tenants
    stamp = {"source": "seller", "captured_via": "import", "updated_at": "2026-10-07T00:00:00Z"}
    async with unit_of_work(b) as session:
        with pytest.raises(CrossTenantQueryError):
            await ProductRepository(session, a).upsert_by_sku(
                ProductUpsert(sku="ISO-1", title="T", category="kurtas", stamp=stamp)
            )
        with pytest.raises(CrossTenantQueryError):
            await ImportRowRepository(session, a).write(
                world.rows["imports"][a], world.rows["imports"][a], RowWrite(2, "failed", {})
            )
        with pytest.raises(CrossTenantQueryError):
            await ImportRepository(session, a).lock_tenant_imports()


async def test_fact_writer_other_tenant_product_is_missing(
    iso_settings: None, world: World, admin_conn: Conn
) -> None:
    from app.core.errors import RowCountError
    from app.db.session import unit_of_work
    from app.repositories.facts import FactWriter

    a, b = world.tenants
    product = admin_conn.execute(
        "SELECT id FROM products WHERE tenant_id = %s LIMIT 1", [b]
    ).fetchone()
    assert product is not None
    product_id: Any = product[0]
    fact = {"value": "x", "source": "seller", "captured_via": "import", "updated_at": "z"}
    with pytest.raises(RowCountError):
        async with unit_of_work(a) as session:
            await FactWriter(session, a).set_attribute(product_id, "fit", fact)


async def test_import_family_reads_are_tenant_scoped(
    iso_settings: None, world: World, admin_conn: Conn
) -> None:
    """The round-1 family reads: another tenant's imported rows, product SKU, size run and
    colourways read as missing, and a size-run write to its product touches nothing."""
    from app.core.errors import RowCountError
    from app.db.session import unit_of_work
    from app.repositories.imports import ImportRowRepository
    from app.repositories.products import ProductRepository
    from app.repositories.variants import ColourwayRepository

    a, b = world.tenants
    product = admin_conn.execute(
        "SELECT id, sku FROM products WHERE tenant_id = %s LIMIT 1", [b]
    ).fetchone()
    assert product is not None
    product_id: Any = product[0]
    sku = str(product[1])
    theirs = world.rows["imports"][b]
    async with unit_of_work(b) as session:
        assert await ProductRepository(session, b).id_of_sku(sku) == product_id
    async with unit_of_work(a) as session:
        rows = ImportRowRepository(session, a)
        assert await rows.imported_family_rows(theirs, [sku, "S"]) == []
        products = ProductRepository(session, a)
        assert await products.id_of_sku(sku) is None
        assert await products.family_of_sku(sku) is None
        assert await products.size_run_of(product_id) == []
        assert await ColourwayRepository(session, a).live_ids(product_id) == []
    with pytest.raises(RowCountError):
        async with unit_of_work(a) as session:
            await ProductRepository(session, a).extend_size_run(product_id, ["m"])
