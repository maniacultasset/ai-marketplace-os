"""S9 T4 (AC 3): the tenant guard on `AppSession`. A statement that touches a TenantScoped
table anywhere in its tree raises `CrossTenantQueryError` unless it is stamped by the repository
base or runs under `platform_scope`; every ORM flush must stay inside the session's tenant."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import delete, exists, func, select, table, update

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("db_settings", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]


@pytest.fixture
def two_tenants(superuser_conn: Conn) -> tuple[UUID, UUID]:
    return f.tenant(superuser_conn), f.tenant(superuser_conn)


def _listing_cte() -> Any:
    from app.db.models.listings import Listing

    return select(Listing).cte()


def _shapes() -> dict[str, Callable[[], Any]]:
    from app.db.models.listings import Listing
    from app.db.models.platform import CategoryTemplate

    return {
        "join": lambda: select(CategoryTemplate).join(Listing, Listing.id == CategoryTemplate.id),
        "subquery": lambda: select(CategoryTemplate).where(
            CategoryTemplate.id.in_(select(Listing.id))
        ),
        "exists": lambda: select(CategoryTemplate).where(exists().where(Listing.id.is_not(None))),
        "cte": lambda: select(_listing_cte().c.id),
        "select_from": lambda: select(func.count()).select_from(Listing),
        "core_table": lambda: select(Listing.__table__),
        "table_clause": lambda: select(func.count()).select_from(table("listings")),
    }


async def test_guard_raises_unscoped() -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.models.listings import Listing
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        with pytest.raises(CrossTenantQueryError):
            await session.execute(select(Listing))


async def test_guard_raises_unscoped_update() -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        with pytest.raises(CrossTenantQueryError):
            await session.execute(update(Product).values(title="x"))


async def test_guard_raises_unscoped_delete() -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        with pytest.raises(CrossTenantQueryError):
            await session.execute(delete(Product))


@pytest.mark.parametrize(
    "shape", ["join", "subquery", "exists", "cte", "select_from", "core_table", "table_clause"]
)
async def test_guard_raises_referenced_tenant_table(shape: str) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        with pytest.raises(CrossTenantQueryError):
            await session.execute(_shapes()[shape]())


async def test_guard_raises_on_lazy_load(
    superuser_conn: Conn, two_tenants: tuple[UUID, UUID]
) -> None:
    """A test-only registry maps `listings` with a lazy="select" relationship: the lazy load is
    a fresh, unstamped statement on `listing_versions`, so the guard refuses it."""
    from app.db.guard import SCOPE_OK, CrossTenantQueryError
    from app.db.models._tenant import TenantScoped
    from app.db.session import get_sessionmaker
    from sqlalchemy import ForeignKey, Uuid
    from sqlalchemy.orm import DeclarativeBase, mapped_column, relationship

    class TestBase(DeclarativeBase):
        pass

    class LazyListing(TenantScoped, TestBase):
        __tablename__ = "listings"
        id = mapped_column(Uuid, primary_key=True)
        tenant_id = mapped_column(Uuid)
        versions = relationship("LazyVersion", lazy="select")

    class LazyVersion(TenantScoped, TestBase):
        __tablename__ = "listing_versions"
        id = mapped_column(Uuid, primary_key=True)
        tenant_id = mapped_column(Uuid)
        listing_id = mapped_column(Uuid, ForeignKey("listings.id"))

    tenant_a, _ = two_tenants
    listing_id = f.listing(superuser_conn, tenant_a)
    async with get_sessionmaker()() as session:
        stmt = (
            select(LazyListing)
            .where(LazyListing.tenant_id == tenant_a)
            .execution_options(**{SCOPE_OK: True})
        )
        row = (await session.execute(stmt)).scalar_one()
        assert row.id == listing_id
        with pytest.raises(CrossTenantQueryError):
            await session.run_sync(lambda _: row.versions)


async def test_guard_passes_with_scope_option(
    superuser_conn: Conn, two_tenants: tuple[UUID, UUID]
) -> None:
    from app.db.guard import SCOPE_OK
    from app.db.models.catalog import Product
    from app.db.session import get_sessionmaker

    tenant_a, _ = two_tenants
    product_id = f.product(superuser_conn, tenant_a)
    async with get_sessionmaker()() as session:
        stmt = (
            select(Product)
            .where(Product.tenant_id == tenant_a)
            .execution_options(**{SCOPE_OK: True})
        )
        assert [p.id for p in (await session.execute(stmt)).scalars()] == [product_id]


async def test_guard_ignores_insert(two_tenants: tuple[UUID, UUID]) -> None:
    from app.db.models.catalog import Product
    from app.db.session import get_sessionmaker
    from sqlalchemy import insert

    tenant_a, _ = two_tenants
    async with get_sessionmaker()() as session:
        await session.execute(
            insert(Product).values(
                tenant_id=tenant_a,
                sku="INS-1",
                title="t",
                category="t_shirts",
                provenance={"title": f.fact(), "category": f.fact()},
            )
        )
        await session.rollback()


async def test_guard_ignores_unscoped_platform_table() -> None:
    from app.db.models.platform import CategoryTemplate
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        assert (await session.execute(select(CategoryTemplate))).all() == []


async def test_guard_error_holds_no_params() -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import get_sessionmaker

    secret = UUID("12345678-1234-5678-1234-567812345678")
    async with get_sessionmaker()() as session:
        with pytest.raises(CrossTenantQueryError) as info:
            await session.execute(select(Product).where(Product.tenant_id == secret))
    assert str(secret) not in str(info.value)
    assert "products" in str(info.value)


async def _scoped_product(session: Any, tenant_id: UUID) -> Any:
    from app.db.guard import SCOPE_OK
    from app.db.models.catalog import Product

    stmt = (
        select(Product).where(Product.tenant_id == tenant_id).execution_options(**{SCOPE_OK: True})
    )
    return (await session.execute(stmt)).scalar_one()


async def test_guard_allows_refresh_of_loaded_object(
    superuser_conn: Conn, two_tenants: tuple[UUID, UUID]
) -> None:
    from app.db.session import get_sessionmaker

    tenant_a, _ = two_tenants
    f.product(superuser_conn, tenant_a, title="Loaded")
    async with get_sessionmaker()() as session:
        product = await _scoped_product(session, tenant_a)
        session.expire(product)
        assert await session.run_sync(lambda _: product.title) == "Loaded"
        await session.refresh(product)
        assert product.title == "Loaded"


async def test_guard_refresh_after_rollback_runs(
    superuser_conn: Conn, two_tenants: tuple[UUID, UUID]
) -> None:
    from app.db.session import get_sessionmaker

    tenant_a, _ = two_tenants
    f.product(superuser_conn, tenant_a, title="Kept")
    async with get_sessionmaker()() as session:
        product = await _scoped_product(session, tenant_a)
        await session.rollback()  # expires every loaded object
        assert await session.run_sync(lambda _: product.title) == "Kept"


def _count(conn: Conn, table: str, tenant_id: UUID) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(sql.Identifier(table))
    row = conn.execute(query, [tenant_id]).fetchone()
    assert row is not None
    return int(row[0])


def _new_product(tenant_id: UUID, sku: str) -> Any:
    from app.db.models.catalog import Product

    return Product(
        tenant_id=tenant_id,
        sku=sku,
        title="t",
        category="t_shirts",
        provenance={"title": f.fact(), "category": f.fact()},
    )


async def test_flush_rejects_insert_other_tenant(
    superuser_conn: Conn, two_tenants: tuple[UUID, UUID]
) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.session import unit_of_work

    tenant_a, tenant_b = two_tenants
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            session.add(_new_product(tenant_b, "OTHER-1"))
            await session.flush()
    assert _count(superuser_conn, "products", tenant_b) == 0


async def test_flush_rejects_tenant_id_change(
    superuser_conn: Conn, two_tenants: tuple[UUID, UUID]
) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.session import unit_of_work

    tenant_a, tenant_b = two_tenants
    f.product(superuser_conn, tenant_a)
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            product = await _scoped_product(session, tenant_a)
            product.tenant_id = tenant_b
            await session.flush()
    assert _count(superuser_conn, "products", tenant_b) == 0


async def test_flush_rejects_delete_other_tenant_object(
    superuser_conn: Conn, two_tenants: tuple[UUID, UUID]
) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.session import get_sessionmaker, unit_of_work

    tenant_a, tenant_b = two_tenants
    f.product(superuser_conn, tenant_b)
    async with get_sessionmaker()() as other:
        foreign = await _scoped_product(other, tenant_b)
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            merged = await session.merge(foreign, load=False)
            await session.delete(merged)
            await session.flush()
    assert _count(superuser_conn, "products", tenant_b) == 1


async def test_flush_own_tenant_passes(
    superuser_conn: Conn, two_tenants: tuple[UUID, UUID]
) -> None:
    from app.db.session import unit_of_work

    tenant_a, _ = two_tenants
    async with unit_of_work(tenant_a) as session:
        session.add(_new_product(tenant_a, "OWN-1"))
    assert _count(superuser_conn, "products", tenant_a) == 1


async def test_flush_without_tenant_context_raises(two_tenants: tuple[UUID, UUID]) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.session import get_sessionmaker

    tenant_a, _ = two_tenants
    async with get_sessionmaker()() as session:
        session.add(_new_product(tenant_a, "RAW-1"))
        with pytest.raises(CrossTenantQueryError):
            await session.flush()
