"""S9 T7 (AC 9, 12): `TenantScopedRepository` filters every read and write on the tenant, reads
another tenant's row as missing (D7), maps constraint errors, and refuses an UPDATE that does
not touch exactly one row (DATABASE.md:133)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import func, select

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("db_settings", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]


def _product_repository() -> type[Any]:
    from app.db.models.catalog import Product
    from app.repositories.base import TenantScopedRepository

    class ProductRepository(TenantScopedRepository[Product]):
        model = Product

    return ProductRepository


def _audit_repository() -> type[Any]:
    from app.db.models.audit_brand import AuditLog
    from app.repositories.base import TenantScopedRepository

    class AuditRepository(TenantScopedRepository[AuditLog]):
        model = AuditLog

    return AuditRepository


def _new_product(sku: str, tenant_id: UUID | None = None) -> Any:
    from app.db.models.catalog import Product

    return Product(
        tenant_id=tenant_id,
        sku=sku,
        title="t",
        category="t_shirts",
        provenance={"title": f.fact(), "category": f.fact()},
    )


@pytest.fixture
def world(superuser_conn: Conn) -> dict[str, Any]:
    a, b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    return {
        "a": a,
        "b": b,
        "a_products": [f.product(superuser_conn, a, sku=f"A-{i}") for i in range(3)],
        "b_product": f.product(superuser_conn, b, sku="B-0"),
    }


async def test_repository_get_own(world: dict[str, Any]) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        found = await _product_repository()(session, world["a"]).get_by_id(world["a_products"][0])
        assert found is not None and found.sku == "A-0"


async def test_repository_get_missing_none(world: dict[str, Any]) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        assert await _product_repository()(session, world["a"]).get_by_id(uuid4()) is None


async def test_repository_get_other_tenant_same_as_missing(world: dict[str, Any]) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        assert (
            await _product_repository()(session, world["a"]).get_by_id(world["b_product"]) is None
        )


async def test_repository_list_excludes_other_tenant(world: dict[str, Any]) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        rows = await _product_repository()(session, world["a"]).list()
    assert sorted(r.id for r in rows) == sorted(world["a_products"])


async def test_repository_list_limit(world: dict[str, Any]) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        assert len(await _product_repository()(session, world["a"]).list(limit=2)) == 2


@pytest.mark.parametrize("limit", [0, 501, True, -1, 1.5])
async def test_repository_list_limit_bounds(world: dict[str, Any], limit: Any) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        with pytest.raises(ValueError, match="limit"):
            await _product_repository()(session, world["a"]).list(limit=limit)


@pytest.mark.parametrize("bad", [None, "8f7a4bd2-0000-4000-8000-000000000000"])
async def test_repository_rejects_non_uuid_tenant(world: dict[str, Any], bad: Any) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        with pytest.raises(ValueError, match="tenant_id"):
            _product_repository()(session, bad)


async def test_repository_add_sets_tenant(superuser_conn: Conn, world: dict[str, Any]) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        added = await _product_repository()(session, world["a"]).add(_new_product("NEW-1"))
        assert added.tenant_id == world["a"]
        assert added.id is not None
    row = superuser_conn.execute("SELECT tenant_id FROM products WHERE sku = 'NEW-1'").fetchone()
    assert row == (world["a"],)


async def test_repository_add_other_tenant_refused(
    superuser_conn: Conn, world: dict[str, Any]
) -> None:
    from app.db.session import unit_of_work

    with pytest.raises(ValueError, match="another tenant"):
        async with unit_of_work(world["a"]) as session:
            await _product_repository()(session, world["a"]).add(_new_product("X-1", world["b"]))
    row = superuser_conn.execute("SELECT count(*) FROM products WHERE sku = 'X-1'").fetchone()
    assert row == (0,)


async def test_repository_add_conflict_mapped(world: dict[str, Any]) -> None:
    from app.core.errors import ConflictError
    from app.db.session import unit_of_work
    from mkos_schemas.codes import ErrorCode

    with pytest.raises(ConflictError) as info:
        async with unit_of_work(world["a"]) as session:
            repo = _product_repository()(session, world["a"])
            try:
                await repo.add(_new_product("A-0"))
            except ConflictError as exc:
                raised_inside = exc
                raise
    assert raised_inside is info.value
    assert info.value.code is ErrorCode.SKU_CONFLICT


async def test_repository_update_one_ok(superuser_conn: Conn, world: dict[str, Any]) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        await _product_repository()(session, world["a"]).update_one(
            world["a_products"][0], title="Renamed"
        )
    row = superuser_conn.execute(
        "SELECT title FROM products WHERE id = %s", [world["a_products"][0]]
    ).fetchone()
    assert row == ("Renamed",)


async def test_repository_update_one_missing_rowcount_error(world: dict[str, Any]) -> None:
    from app.core.errors import RowCountError
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        with pytest.raises(RowCountError) as info:
            await _product_repository()(session, world["a"]).update_one(uuid4(), title="x")
    assert (info.value.expected, info.value.actual) == (1, 0)


async def test_repository_update_one_other_tenant_rowcount_error(
    superuser_conn: Conn, world: dict[str, Any]
) -> None:
    from app.core.errors import RowCountError
    from app.db.session import unit_of_work

    with pytest.raises(RowCountError):
        async with unit_of_work(world["a"]) as session:
            await _product_repository()(session, world["a"]).update_one(
                world["b_product"], title="Hijacked"
            )
    row = superuser_conn.execute(
        "SELECT title FROM products WHERE id = %s", [world["b_product"]]
    ).fetchone()
    assert row != ("Hijacked",)


@pytest.mark.parametrize(
    "values", [{}, {"tenant_id": uuid4()}, {"id": uuid4()}, {"no_such_column": 1}]
)
async def test_repository_update_one_rejects_tenant_id_and_unknown_columns(
    world: dict[str, Any], values: dict[str, Any]
) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        with pytest.raises(ValueError):
            await _product_repository()(session, world["a"]).update_one(
                world["a_products"][0], **values
            )


async def test_repository_update_one_conflict_mapped(world: dict[str, Any]) -> None:
    from app.core.errors import ConflictError
    from app.db.session import unit_of_work
    from mkos_schemas.codes import ErrorCode

    with pytest.raises(ConflictError) as info:
        async with unit_of_work(world["a"]) as session:
            await _product_repository()(session, world["a"]).update_one(
                world["a_products"][0], sku="A-1"
            )
    assert info.value.code is ErrorCode.SKU_CONFLICT


async def test_repository_include_platform_refused_for_products(world: dict[str, Any]) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        with pytest.raises(ValueError, match="platform"):
            _product_repository()(session, world["a"])._select(include_platform=True)


async def test_rule_repository_effective_own_plus_platform_not_other(
    superuser_conn: Conn, world: dict[str, Any]
) -> None:
    from app.db.session import unit_of_work
    from app.repositories.rules import RuleRepository

    platform = f.rule(superuser_conn, None)
    own = f.rule(superuser_conn, world["a"])
    f.rule(superuser_conn, world["b"])
    f.rule(superuser_conn, world["a"], marketplace="flipkart")
    disabled = f.rule(superuser_conn, None, field="content.bullets", enabled=False)
    async with unit_of_work(world["a"]) as session:
        rules = await RuleRepository(session, world["a"]).list_effective("amazon", "t_shirts")
    ids = {r.id for r in rules}
    assert ids == {platform, own}
    assert disabled not in ids


async def test_audit_log_null_tenant_rows_hidden(
    superuser_conn: Conn, world: dict[str, Any]
) -> None:
    from app.db.session import unit_of_work

    own = f.audit(superuser_conn, world["a"])
    f.audit(superuser_conn, None)
    f.audit(superuser_conn, world["b"])
    async with unit_of_work(world["a"]) as session:
        rows = await _audit_repository()(session, world["a"]).list()
    assert [r.id for r in rows] == [own]


async def test_update_one_then_read_trigger_updated_at(world: dict[str, Any]) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        repo = _product_repository()(session, world["a"])
        product = await repo.get_by_id(world["a_products"][0])
        assert product is not None
        before = product.updated_at
        await repo.update_one(product.id, title="Touched")
        now = await session.scalar(select(func.now()))
        assert await session.run_sync(lambda _: product.title) == "Touched"
        after = await session.run_sync(lambda _: product.updated_at)
    assert after == now
    assert after > before


async def test_scoped_outer_rejects_foreign_inner(world: dict[str, Any]) -> None:
    from app.db.models.catalog import Product
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        repo = _product_repository()(session, world["a"])
        other = _product_repository()(session, world["b"])
        for inner in (select(Product).cte(), other._select().cte()):
            with pytest.raises(TypeError, match="not built"):
                repo._scoped_outer(select(inner.c.id), inner)


async def test_scoped_outer_rejects_extra_tenant_table(world: dict[str, Any]) -> None:
    from app.db.models.listings import Listing
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        repo = _product_repository()(session, world["a"])
        inner = repo._select().cte()
        outer = select(inner.c.id).join(Listing, Listing.product_id == inner.c.id)
        with pytest.raises(TypeError, match="outside"):
            repo._scoped_outer(outer, inner)


async def test_scoped_outer_runs_own_inner(world: dict[str, Any]) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(world["a"]) as session:
        repo = _product_repository()(session, world["a"])
        inner = repo._select().where(repo.model.sku != "A-0").cte()
        stmt = repo._scoped_outer(select(inner.c.sku).order_by(inner.c.sku), inner)
        assert list((await session.execute(stmt)).scalars()) == ["A-1", "A-2"]
