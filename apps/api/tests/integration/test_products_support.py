"""S12 T8: the products route suites' support: minted bearers authenticate as the row's role,
the broken variants are 401, the app runs as `app_rw`, and the tenant limiter is reset."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.support.rate_limits import reset_rate_limits

from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bad_bearer, bearer_for

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


async def test_bearer_authenticates_on_auth_me(api: httpx.AsyncClient, products_world: Any) -> None:
    response = await api.get(
        "/api/v1/auth/me", headers=bearer_for(products_world, "reviewer@acme.test", role="owner")
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "reviewer"


@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_bad_bearer_variants_401(
    api: httpx.AsyncClient, products_world: Any, variant: str
) -> None:
    response = await api.get("/api/v1/auth/me", headers=bad_bearer(products_world, variant))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


async def test_api_client_runs_as_app_rw(api: httpx.AsyncClient, products_world: Any) -> None:
    from app.repositories.transactions import unit_of_work
    from sqlalchemy import func, select

    async with unit_of_work(products_world.tenant("acme")) as session:
        assert await session.scalar(select(func.current_user())) == "app_rw"


async def test_products_world_resets_tenant_limiter(
    api: httpx.AsyncClient, products_world: Any, auth_db: Conn
) -> None:
    headers = bearer_for(products_world, "operator@acme.test")
    assert (await api.get(PRODUCTS, headers=headers)).status_code == 200
    auth_db.execute("UPDATE rate_limit_counters SET n = 1000")
    limited = await api.get(PRODUCTS, headers=headers)
    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "RATE_LIMITED"
    reset_rate_limits(auth_db)
    assert (await api.get(PRODUCTS, headers=headers)).status_code == 200
