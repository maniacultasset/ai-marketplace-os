"""S9 e2e round 2 attacks on the tenant guard: shapes that reference a TenantScoped table and
must still raise `CrossTenantQueryError` when unstamped (AC 3)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from sqlalchemy import literal_column, select, union_all
from sqlalchemy.orm import aliased

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("db_settings", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]


def _shapes() -> dict[str, Any]:
    from app.db.models.catalog import Product
    from app.db.models.listings import Listing
    from app.db.models.platform import CategoryTemplate

    return {
        "aliased": lambda: select(aliased(Listing)),
        "union": lambda: union_all(select(Listing.id), select(CategoryTemplate.id)),
        "scalar_subq": lambda: select(
            CategoryTemplate.id, select(Product.title).limit(1).scalar_subquery()
        ),
        "lateral": lambda: select(CategoryTemplate.id).join(
            select(Listing.id).lateral(), literal_column("true")
        ),
    }


@pytest.mark.parametrize("shape", ["aliased", "union", "scalar_subq", "lateral"])
async def test_guard_raises_more_shapes(shape: str) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        with pytest.raises(CrossTenantQueryError):
            await session.execute(_shapes()[shape]())


async def test_session_get_other_tenant_refused(superuser_conn: Conn) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.models.catalog import Product
    from app.db.session import unit_of_work

    tenant_a, tenant_b = f.tenant(superuser_conn), f.tenant(superuser_conn)
    b_product = f.product(superuser_conn, tenant_b)
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(tenant_a) as session:
            await session.get(Product, b_product)
