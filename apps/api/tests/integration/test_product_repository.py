"""S12 T5 (AC1, AC3; DATABASE.md:424; BACKEND-ARCHITECTURE.md:1410): `ProductRepository` reads
live rows of its own tenant only, and pages them by keyset `(created_at DESC, id DESC)`, never
OFFSET, as `app_rw` under FORCE RLS."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

from .auth_support import World, build_world
from .conftest import LOCAL_HOSTS
from .products_support import Conn, insert_products, soft_delete, statements

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _acme(world: World) -> uuid.UUID:
    return world.tenant("acme")


async def _create(world: World, sku: str) -> Any:
    from app.repositories.products import ProductRepository
    from app.repositories.transactions import unit_of_work

    tenant = _acme(world)
    operator = world.user("operator@acme.test")
    stamp = {"source": "seller", "captured_via": "api", "updated_at": "2026-10-06T00:00:00Z"}
    async with unit_of_work(tenant, user_id=operator) as session:
        return await ProductRepository(session, tenant).create(
            sku=sku,
            title="A title",
            category="kurtas",
            attributes={},
            provenance={"title": stamp, "category": stamp},
            created_by=operator,
        )


async def _get(tenant: uuid.UUID, product_id: uuid.UUID) -> Any:
    from app.repositories.products import ProductRepository
    from app.repositories.transactions import unit_of_work

    async with unit_of_work(tenant) as session:
        return await ProductRepository(session, tenant).get_by_id(product_id)


async def _page(
    tenant: uuid.UUID,
    *,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None = None,
    sort_in_memory: bool = False,
) -> tuple[list[Any], bool]:
    from app.repositories.products import ProductRepository
    from app.repositories.transactions import unit_of_work
    from sqlalchemy import text

    async with unit_of_work(tenant) as session:
        if sort_in_memory:
            # without the keyset index the planner sorts, so only ORDER BY decides tie order
            await session.execute(text("SET LOCAL enable_indexscan = off"))
            await session.execute(text("SET LOCAL enable_bitmapscan = off"))
        return await ProductRepository(session, tenant).list_page(limit=limit, after=after)


def _keyset(conn: Conn, tenant: uuid.UUID) -> list[uuid.UUID]:
    rows = conn.execute(
        "SELECT id FROM products WHERE tenant_id = %s AND deleted_at IS NULL "
        "ORDER BY created_at DESC, id DESC",
        [tenant],
    ).fetchall()
    return [r[0] for r in rows]


async def test_add_then_get(world: World) -> None:
    created = await _create(world, "S12-ADD")
    assert created.tenant_id == _acme(world)
    assert created.created_by == world.user("operator@acme.test")
    found = await _get(_acme(world), created.id)
    assert found is not None and found.sku == "S12-ADD" and found.status == "draft"


async def test_get_other_tenant_none(world: World) -> None:
    created = await _create(world, "S12-MINE")
    assert await _get(world.tenant("bharat"), created.id) is None


async def test_get_soft_deleted_none(world: World, auth_db: Conn) -> None:
    created = await _create(world, "S12-GONE")
    soft_delete(auth_db, created.id)
    assert await _get(_acme(world), created.id) is None


async def test_list_page_order_desc(world: World, auth_db: Conn) -> None:
    insert_products(auth_db, _acme(world), 3, prefix="OLD")
    insert_products(auth_db, _acme(world), 2, prefix="NEW")
    rows, has_more = await _page(_acme(world), limit=10)
    assert [r.id for r in rows] == _keyset(auth_db, _acme(world))
    assert [r.sku.split("-")[0] for r in rows][:2] == ["NEW", "NEW"]
    assert has_more is False


async def test_list_page_equal_created_at_tiebreak_by_id(world: World, auth_db: Conn) -> None:
    ids = insert_products(auth_db, _acme(world), 6)
    rows, _ = await _page(_acme(world), limit=10, sort_in_memory=True)
    assert len({r.created_at for r in rows}) == 1
    assert [r.id for r in rows] == sorted(ids, reverse=True)


async def test_list_page_after_excludes_cursor_row(world: World, auth_db: Conn) -> None:
    insert_products(auth_db, _acme(world), 5)
    first, _ = await _page(_acme(world), limit=2)
    cursor = (first[-1].created_at, first[-1].id)
    second, _ = await _page(_acme(world), limit=2, after=cursor)
    assert first[-1].id not in {r.id for r in second}
    assert [r.id for r in first + second] == _keyset(auth_db, _acme(world))[:4]


async def test_list_page_has_more(world: World, auth_db: Conn) -> None:
    insert_products(auth_db, _acme(world), 3)
    assert (await _page(_acme(world), limit=2))[1] is True
    assert (await _page(_acme(world), limit=3))[1] is False
    assert (await _page(_acme(world), limit=4))[1] is False


async def test_list_page_walk_every_row_once(world: World, auth_db: Conn) -> None:
    insert_products(auth_db, _acme(world), 60, prefix="A")
    insert_products(auth_db, _acme(world), 60, prefix="B")
    sizes: list[int] = []
    seen: list[uuid.UUID] = []
    after: tuple[datetime, uuid.UUID] | None = None
    while True:
        rows, has_more = await _page(_acme(world), limit=50, after=after, sort_in_memory=True)
        sizes.append(len(rows))
        seen += [r.id for r in rows]
        if not has_more:
            break
        after = (rows[-1].created_at, rows[-1].id)
    assert sizes == [50, 50, 20]
    assert seen == _keyset(auth_db, _acme(world))
    assert len(set(seen)) == 120


async def test_list_page_excludes_soft_deleted(world: World, auth_db: Conn) -> None:
    ids = insert_products(auth_db, _acme(world), 3)
    soft_delete(auth_db, ids[0])
    rows, _ = await _page(_acme(world), limit=10)
    assert ids[0] not in {r.id for r in rows}
    assert len(rows) == 2


async def test_list_page_excludes_other_tenant(world: World, auth_db: Conn) -> None:
    insert_products(auth_db, world.tenant("bharat"), 4, prefix="BH")
    mine = insert_products(auth_db, _acme(world), 2, prefix="AC")
    rows, _ = await _page(_acme(world), limit=50)
    assert {r.id for r in rows} == set(mine)
    assert {r.tenant_id for r in rows} == {_acme(world)}


@pytest.mark.parametrize("limit", [0, 201, True, -1, 2.0])
async def test_list_page_limit_bounds(world: World, limit: Any) -> None:
    from app.repositories.products import ProductRepository

    class _NoSql:
        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("SQL issued")

    repo = ProductRepository(_NoSql(), _acme(world))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        await repo.list_page(limit=limit, after=None)


async def test_list_page_sql_has_no_offset(world: World, auth_db: Conn) -> None:
    insert_products(auth_db, _acme(world), 3)
    rows, _ = await _page(_acme(world), limit=1)
    after = (rows[0].created_at, rows[0].id)
    with statements() as seen:
        await _page(_acme(world), limit=1, after=after)
    product_sql = [s for s in seen if "FROM products" in s]
    assert product_sql
    assert all("OFFSET" not in s.upper() for s in product_sql)
    assert all("LIMIT" in s.upper() for s in product_sql)
