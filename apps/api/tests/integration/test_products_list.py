"""S12 T12 (AC2, AC3): `GET /api/v1/products`, keyset pages over `(created_at DESC, id DESC)`
with an opaque cursor and no OFFSET; only the caller's tenant's live rows."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import (
    PRODUCTS,
    Conn,
    bad_bearer,
    bearer_for,
    insert_products,
    site_body,
    soft_delete,
    statements,
    without_correlation,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _acme(world: World, role: str = "operator") -> dict[str, str]:
    return bearer_for(world, f"{role}@acme.test")


def _keyset(conn: Conn, tenant: Any) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM products WHERE tenant_id = %s AND deleted_at IS NULL "
        "ORDER BY created_at DESC, id DESC",
        [tenant],
    ).fetchall()
    return [str(r[0]) for r in rows]


async def _walk(
    api: httpx.AsyncClient, headers: dict[str, str], limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    params: dict[str, Any] = {"limit": limit}
    while True:
        response = await api.get(PRODUCTS, params=params, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        items += body["data"]
        pages.append(body["page"])
        if not body["page"]["has_more"]:
            return items, pages
        params = {"limit": limit, "cursor": body["page"]["next_cursor"]}


@pytest.mark.endpoint("products_list", case="success")
async def test_products_list_success(api: httpx.AsyncClient, products_world: World) -> None:
    created = await api.post(PRODUCTS, json=site_body("S12-L1"), headers=_acme(products_world))
    assert created.status_code == 201
    response = await api.get(PRODUCTS, headers=_acme(products_world, "reviewer"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"] == [created.json()]
    assert body["page"] == {"next_cursor": None, "has_more": False, "limit": 50}


@pytest.mark.endpoint("products_list", case="validation")
@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 201}, {"cursor": "eyJ0IjoieCIsImlkIjoieSJ9"}],
    ids=["limit_0", "limit_201", "tampered_cursor"],
)
async def test_products_list_validation(
    api: httpx.AsyncClient, products_world: World, params: dict[str, Any]
) -> None:
    response = await api.get(PRODUCTS, params=params, headers=_acme(products_world))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


@pytest.mark.endpoint("products_list", case="missing_auth")
async def test_products_list_missing_auth(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.get(PRODUCTS)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def _invalid(api: httpx.AsyncClient, world: World, variant: str) -> None:
    response = await api.get(PRODUCTS, headers=bad_bearer(world, variant))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("products_list", case="invalid_token", variant="expired")
async def test_products_list_invalid_token_expired(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "expired")


@pytest.mark.endpoint("products_list", case="invalid_token", variant="bad_signature")
async def test_products_list_invalid_token_bad_signature(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "bad_signature")


@pytest.mark.endpoint("products_list", case="invalid_token", variant="wrong_typ")
async def test_products_list_invalid_token_wrong_typ(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "wrong_typ")


@pytest.mark.endpoint("products_list", case="wrong_tenant")
@pytest.mark.parametrize("kind", ["no_foreign_rows", "foreign_cursor"])
async def test_products_list_wrong_tenant(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, kind: str
) -> None:
    bharat = products_world.tenant("bharat")
    insert_products(auth_db, bharat, 3, prefix="BH")
    mine = set(insert_products(auth_db, products_world.tenant("acme"), 2, prefix="AC"))
    params: dict[str, Any] = {}
    if kind == "foreign_cursor":
        first = await api.get(
            PRODUCTS, params={"limit": 1}, headers=bearer_for(products_world, "owner@bharat.test")
        )
        params = {"cursor": first.json()["page"]["next_cursor"]}
    response = await api.get(PRODUCTS, params=params, headers=_acme(products_world))
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert ids <= {str(i) for i in mine}
    assert not any(item["sku"].startswith("BH") for item in response.json()["data"])


async def test_products_list_keyset_walk(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    insert_products(auth_db, acme, 120)  # one statement: every row shares one created_at
    items, pages = await _walk(api, _acme(products_world), 50)
    assert [len(p) for p in [items[:50], items[50:100], items[100:]]] == [50, 50, 20]
    assert len(pages) == 3
    assert [item["id"] for item in items] == _keyset(auth_db, acme)
    assert len({item["id"] for item in items}) == 120
    assert pages[-1] == {"next_cursor": None, "has_more": False, "limit": 50}
    assert all(p["has_more"] and p["next_cursor"] for p in pages[:-1])


async def test_products_list_keyset_soft_deleted(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    ids = insert_products(auth_db, acme, 5)
    soft_delete(auth_db, ids[2])
    items, _ = await _walk(api, _acme(products_world), 2)
    assert str(ids[2]) not in {item["id"] for item in items}
    assert len(items) == 4


async def test_products_list_no_offset(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    insert_products(auth_db, products_world.tenant("acme"), 3)
    first = await api.get(PRODUCTS, params={"limit": 1}, headers=_acme(products_world))
    cursor = first.json()["page"]["next_cursor"]
    with statements() as seen:
        response = await api.get(
            PRODUCTS, params={"limit": 1, "cursor": cursor}, headers=_acme(products_world)
        )
    assert response.status_code == 200
    product_sql = [s for s in seen if "FROM products" in s]
    assert product_sql
    assert all("OFFSET" not in s.upper() for s in product_sql)


async def test_products_list_tid_without_membership(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    insert_products(auth_db, products_world.tenant("bharat"), 2, prefix="BH")
    forged = bearer_for(products_world, "operator@acme.test", tenant="bharat")
    response = await api.get(PRODUCTS, headers=forged)
    assert response.status_code == 401
    missing = await api.get(PRODUCTS)
    assert without_correlation(response.json()) == without_correlation(missing.json())
    assert "BH-" not in response.text


async def test_products_crud_create_get_list(api: httpx.AsyncClient, products_world: World) -> None:
    """AC1 (Done-when 1): an acme operator creates, reads and lists a product; bharat cannot."""
    created = await api.post(PRODUCTS, json=site_body("S12-CRUD"), headers=_acme(products_world))
    assert created.status_code == 201
    product_id = created.json()["id"]
    got = await api.get(f"{PRODUCTS}/{product_id}", headers=_acme(products_world))
    assert got.status_code == 200 and got.json() == created.json()
    listed = await api.get(PRODUCTS, headers=_acme(products_world))
    assert product_id in {item["id"] for item in listed.json()["data"]}
    bharat = bearer_for(products_world, "operator@bharat.test")
    assert (await api.get(f"{PRODUCTS}/{product_id}", headers=bharat)).status_code == 404
    bharat_list = await api.get(PRODUCTS, headers=bharat)
    assert product_id not in {item["id"] for item in bharat_list.json()["data"]}
