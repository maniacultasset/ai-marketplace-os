"""S9 fix round 2 (F0-F2, AC 3): shapes that reference a TenantScoped table without the mapped
`Table` object, or read one as an INSERT source, must still raise `CrossTenantQueryError` when
unstamped, and must not leak or copy another tenant's rows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import column, insert, literal, select, table

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("db_settings", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]


async def test_guard_raises_lightweight_table_clause(superuser_conn: Conn) -> None:
    """`sqlalchemy.table("products", ...)` names a TenantScoped table without the mapped
    `Table` object; tenant A's unit of work must not read tenant B's rows through it."""
    from app.db.guard import CrossTenantQueryError
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, tenant_b)
    products = table("products", column("tenant_id"), column("sku"))
    leaked: list[Any] = []
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            leaked = list((await session.execute(select(products.c.tenant_id))).all())
    assert UUID(str(tenant_b)) not in {row[0] for row in leaked}


async def test_guard_raises_insert_from_select_of_other_tenant(superuser_conn: Conn) -> None:
    """`insert().from_select()` reads its source like any SELECT: copying tenant B's products
    into tenant A must raise, and nothing lands in tenant A."""
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, tenant_b)
    cols = ["tenant_id", "sku", "title", "category", "provenance"]
    source = select(
        literal(tenant_a, Product.tenant_id.type),
        Product.sku,
        Product.title,
        Product.category,
        Product.provenance,
    ).where(Product.tenant_id == tenant_b)
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.execute(insert(Product).from_select(cols, source))
    row = superuser_conn.execute(
        "SELECT count(*) FROM products WHERE tenant_id = %s", (tenant_a,)
    ).fetchone()
    assert row == (0,)


async def test_guard_raises_insert_from_lightweight_table(superuser_conn: Conn) -> None:
    """The INSERT source check sees a lightweight `table()` too."""
    from app.db.guard import CrossTenantQueryError
    from app.db.session import unit_of_work

    tenant_a = f.tenant(superuser_conn)
    products = table("products", column("tenant_id"), column("sku"))
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.execute(
                insert(products).from_select(["tenant_id", "sku"], select(products))
            )
