"""S9 re-check round 4: an INSERT that reads another tenant through a CTE attached with
`add_cte()` or a scalar subquery wrapped in a SQL function in `.values()` is refused."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from sqlalchemy import func, insert, select

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("db_settings", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]


def _row(tenant_id: object, title: object) -> dict[str, object]:
    return {
        "tenant_id": tenant_id,
        "sku": "copy",
        "title": title,
        "category": "t_shirts",
        "provenance": {"title": f.fact(), "category": f.fact()},
    }


async def test_insert_add_cte_of_other_tenant(superuser_conn: Conn) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, tenant_b)
    stolen = select(Product.title).where(Product.tenant_id == tenant_b).cte("stolen")
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.execute(insert(Product).values(**_row(tenant_a, "x")).add_cte(stolen))


async def test_insert_function_wrapped_subquery_of_other_tenant(superuser_conn: Conn) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, tenant_b)
    agg = select(func.string_agg(Product.title, ",")).where(Product.tenant_id == tenant_b)
    title = func.coalesce(agg.scalar_subquery(), "z")
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.execute(insert(Product).values(**_row(tenant_a, title)))
