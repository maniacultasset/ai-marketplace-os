"""S9 round 3 re-check (AC 3): more lightweight `table()` and INSERT-source shapes must raise
`CrossTenantQueryError` unstamped and leave other tenants' rows untouched."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from sqlalchemy import column, delete, exists, insert, literal, select, table, union_all, update

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("db_settings", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]


def _products() -> Any:
    return table("products", column("tenant_id"), column("sku"), column("title"))


def _shapes() -> dict[str, Any]:
    products = _products()
    qualified = table("products", column("tenant_id"), schema="public")
    tenants = table("tenants", column("id"))
    return {
        "schema_qualified": select(qualified.c.tenant_id),
        "alias": select(products.alias("p").c.sku),
        "union": union_all(select(tenants.c.id), select(products.c.tenant_id)),
        "exists": select(tenants.c.id).where(exists().where(products.c.tenant_id == tenants.c.id)),
        "cte": select(select(products.c.sku).cte("c")),
        "update": update(products).values(title="x"),
        "delete": delete(products),
    }


@pytest.mark.parametrize("shape", sorted(_shapes()))
async def test_guard_raises_table_clause_shape(superuser_conn: Conn, shape: str) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, tenant_b)
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.execute(_shapes()[shape])
    row = superuser_conn.execute(
        "SELECT count(*), min(title) FROM products WHERE tenant_id = %s", (tenant_b,)
    ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] != "x"


async def test_guard_raises_insert_from_select_cte_source(superuser_conn: Conn) -> None:
    """A from_select whose source reads tenant B through a CTE raises; nothing lands in A."""
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, tenant_b)
    src = select(Product).where(Product.tenant_id == tenant_b).cte("src")
    source = select(
        literal(tenant_a, Product.tenant_id.type),
        src.c.sku,
        src.c.title,
        src.c.category,
        src.c.provenance,
    )
    cols = ["tenant_id", "sku", "title", "category", "provenance"]
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.execute(insert(Product).from_select(cols, source))
    row = superuser_conn.execute(
        "SELECT count(*) FROM products WHERE tenant_id = %s", (tenant_a,)
    ).fetchone()
    assert row == (0,)
