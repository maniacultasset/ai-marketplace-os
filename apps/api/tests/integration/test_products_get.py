"""S12 T11 (AC1, AC2): `GET /api/v1/products/{product_id}`. Another tenant's id answers the same
bytes as an id that does not exist (`test_products_cross_tenant_404`, Done-when 1)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import (
    PRODUCT_FIELDS,
    PRODUCTS,
    Conn,
    bad_bearer,
    bearer_for,
    site_body,
    soft_delete,
    without_correlation,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


async def _created(api: httpx.AsyncClient, world: World, sku: str = "S12-GET") -> dict[str, Any]:
    response = await api.post(
        PRODUCTS, json=site_body(sku), headers=bearer_for(world, "operator@acme.test")
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.endpoint("products_get", case="success")
async def test_products_get_success(api: httpx.AsyncClient, products_world: World) -> None:
    created = await _created(api, products_world)
    for role in ("owner", "operator", "reviewer", "admin"):
        response = await api.get(
            f"{PRODUCTS}/{created['id']}", headers=bearer_for(products_world, f"{role}@acme.test")
        )
        assert response.status_code == 200, response.text
        assert response.json() == created
        assert set(response.json()) == PRODUCT_FIELDS


@pytest.mark.endpoint("products_get", case="validation")
@pytest.mark.parametrize("bad", ["not-a-uuid", "1", "00000000-0000-0000-0000-00000000000g"])
async def test_products_get_validation(
    api: httpx.AsyncClient, products_world: World, bad: str
) -> None:
    response = await api.get(
        f"{PRODUCTS}/{bad}", headers=bearer_for(products_world, "operator@acme.test")
    )
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


@pytest.mark.endpoint("products_get", case="missing_auth")
async def test_products_get_missing_auth(api: httpx.AsyncClient, products_world: World) -> None:
    created = await _created(api, products_world)
    response = await api.get(f"{PRODUCTS}/{created['id']}")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "S12-GET" not in response.text


async def _invalid(api: httpx.AsyncClient, world: World, variant: str) -> None:
    created = await _created(api, world)
    response = await api.get(f"{PRODUCTS}/{created['id']}", headers=bad_bearer(world, variant))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("products_get", case="invalid_token", variant="expired")
async def test_products_get_invalid_token_expired(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "expired")


@pytest.mark.endpoint("products_get", case="invalid_token", variant="bad_signature")
async def test_products_get_invalid_token_bad_signature(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "bad_signature")


@pytest.mark.endpoint("products_get", case="invalid_token", variant="wrong_typ")
async def test_products_get_invalid_token_wrong_typ(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "wrong_typ")


@pytest.mark.endpoint("products_get", case="wrong_tenant")
@pytest.mark.parametrize("role", ["owner", "operator", "reviewer", "admin"])
async def test_products_get_wrong_tenant(
    api: httpx.AsyncClient, products_world: World, role: str
) -> None:
    """= `test_products_cross_tenant_404`: a bharat user asking for acme's product."""
    created = await _created(api, products_world)
    headers = {
        **bearer_for(products_world, f"{role}@bharat.test"),
        "X-Request-ID": "req-s12-xtenant-0001",
    }
    foreign = await api.get(f"{PRODUCTS}/{created['id']}", headers=headers)
    missing = await api.get(f"{PRODUCTS}/{uuid.uuid4()}", headers=headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.content == missing.content
    assert foreign.json()["error"]["details"] is None
    assert created["id"] not in foreign.text and "S12-GET" not in foreign.text


test_products_cross_tenant_404 = test_products_get_wrong_tenant


@pytest.mark.endpoint("products_get", case="not_found")
@pytest.mark.parametrize("kind", ["random", "soft_deleted"])
async def test_products_get_not_found(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, kind: str
) -> None:
    if kind == "random":
        product_id = str(uuid.uuid4())
    else:
        product_id = (await _created(api, products_world))["id"]
        soft_delete(auth_db, product_id)
    response = await api.get(
        f"{PRODUCTS}/{product_id}", headers=bearer_for(products_world, "operator@acme.test")
    )
    assert response.status_code == 404
    error = response.json()["error"]
    assert (error["type"], error["message"], error["details"]) == (
        "NOT_FOUND",
        "product_id not found.",
        None,
    )


async def test_products_get_tid_without_membership(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    bharat = await api.post(
        PRODUCTS,
        json=site_body("S12-BH"),
        headers=bearer_for(products_world, "operator@bharat.test"),
    )
    assert bharat.status_code == 201
    forged = bearer_for(products_world, "operator@acme.test", tenant="bharat")
    response = await api.get(f"{PRODUCTS}/{bharat.json()['id']}", headers=forged)
    assert response.status_code == 401
    missing = await api.get(f"{PRODUCTS}/{bharat.json()['id']}")
    assert without_correlation(response.json()) == without_correlation(missing.json())
    assert "S12-BH" not in response.text
