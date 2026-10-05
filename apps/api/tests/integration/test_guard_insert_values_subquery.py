"""S9 fix round 3 (F0-F2): every nested SELECT an INSERT reads (`.values()`, multi-values,
`RETURNING`, `ON CONFLICT DO UPDATE SET/WHERE`) is held to the tenant scope."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("db_settings", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]


async def test_insert_values_scalar_subquery_of_other_tenant(superuser_conn: Conn) -> None:
    """insert(Product).values(title=<scalar subquery over tenant B>) copies B's data into A."""
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, tenant_b)
    stolen = select(Product.title).where(Product.tenant_id == tenant_b).limit(1).scalar_subquery()
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.execute(
                insert(Product).values(
                    tenant_id=tenant_a,
                    sku="copy",
                    title=stolen,
                    category="t_shirts",
                    provenance={"title": f.fact(), "category": f.fact()},
                )
            )


async def test_insert_returning_scalar_subquery_of_other_tenant(superuser_conn: Conn) -> None:
    """RETURNING a scalar subquery over tenant B's rows reads B's data straight back."""
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, tenant_b)
    count_all = select(func.count()).select_from(Product).scalar_subquery()
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.execute(
                pg_insert(Product)
                .values(
                    tenant_id=tenant_a,
                    sku="own",
                    title="t",
                    category="t_shirts",
                    provenance={"title": f.fact(), "category": f.fact()},
                )
                .returning(count_all)
            )


async def test_insert_multi_values_scalar_subquery_of_other_tenant(superuser_conn: Conn) -> None:
    """A scalar subquery in the second row of a multi-values INSERT reads tenant B."""
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, tenant_b)
    stolen = select(Product.title).where(Product.tenant_id == tenant_b).limit(1).scalar_subquery()
    row = {
        "tenant_id": tenant_a,
        "category": "t_shirts",
        "provenance": {"title": f.fact(), "category": f.fact()},
    }
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.execute(
                insert(Product).values(
                    [{**row, "sku": "a", "title": "t"}, {**row, "sku": "b", "title": stolen}]
                )
            )


@pytest.mark.parametrize("part", ["set", "where"])
async def test_insert_on_conflict_subquery_of_other_tenant(superuser_conn: Conn, part: str) -> None:
    """ON CONFLICT DO UPDATE SET/WHERE with a subquery over tenant B reads B unscoped."""
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    f.product(superuser_conn, tenant_b)
    other = select(Product.title).where(Product.tenant_id == tenant_b).limit(1)
    stmt = pg_insert(Product).values(
        tenant_id=tenant_a,
        sku="own",
        title="t",
        category="t_shirts",
        provenance={"title": f.fact(), "category": f.fact()},
    )
    if part == "set":
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "sku"], set_={"title": other.scalar_subquery()}
        )
    else:
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "sku"], set_={"title": "x"}, where=other.exists()
        )
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.execute(stmt)


async def test_plain_values_insert_and_platform_subquery_stay_allowed(
    superuser_conn: Conn,
) -> None:
    """The PLAN §8 residual: a plain `.values()` INSERT (target columns only) is not refused,
    nor is a subquery that reads only a platform (non-tenant) table."""
    from app.db.models.catalog import Product
    from app.db.models.platform import CategoryTemplate
    from app.db.session import unit_of_work

    tenant_a = f.tenant(superuser_conn)
    platform = select(func.count()).select_from(CategoryTemplate).scalar_subquery()
    async with unit_of_work(tenant_a) as session:
        await session.execute(
            pg_insert(Product)
            .values(
                tenant_id=tenant_a,
                sku="own",
                title="t",
                category="t_shirts",
                provenance={"title": f.fact(), "category": f.fact()},
            )
            .returning(platform)
        )
