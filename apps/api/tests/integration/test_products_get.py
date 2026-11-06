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


# ---- SI1 T10 (AC1, D1c): the read model and the ETag ----


def _get(api: httpx.AsyncClient, world: World, product_id: Any) -> Any:
    return api.get(f"{PRODUCTS}/{product_id}", headers=bearer_for(world, "operator@acme.test"))


async def test_products_get_reads_intake_fields_and_etag(
    api: httpx.AsyncClient, products_world: World
) -> None:
    from app.services.products import product_etag

    from . import factories as f

    acme = products_world.tenant("acme")
    conn = products_world.conn
    maker = f.party(
        conn, acme, address_line1="12 MG Road", city="Bengaluru", state_code="KA",
        postal_code="560001", email="care@acme.example", phone="+91 80 1234 5678",
    )  # fmt: skip
    product_id = f.product(
        conn,
        acme,
        style_number="ST-9",
        mrp="1299.00",
        selling_price="999.50",
        manufacturer_party_id=maker,
        attributes={
            "fiber_composition": f.fact(
                value="60% Cotton, 40% Polyester",
                normalized=[{"fibre": "cotton", "percent": 60}, {"fibre": "polyester", "percent": 40}],
            ),
            "size_measurements": {
                "normalized": {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 101}]},
                "source": "seller",
                "captured_via": "api",
                "updated_at": "2026-11-06T00:00:00Z",
            },
            "size_chart_url": f.fact(value="https://cdn.example.com/chart.png"),
        },
        provenance={
            "title": f.fact(), "category": f.fact(), "style_number": f.fact(), "mrp": f.fact(),
            "selling_price": f.fact(), "manufacturer": f.fact(),
        },
    )  # fmt: skip
    response = await _get(api, products_world, product_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["style_number"], body["mrp"], body["selling_price"], body["currency"]) == (
        "ST-9",
        "1299.00",
        "999.50",
        "INR",
    )
    assert body["fiber_composition"] == [
        {"fibre": "cotton", "percent": 60},
        {"fibre": "polyester", "percent": 40},
    ]
    assert body["size_chart"] == {
        "unit": "cm",
        "basis": "garment",
        "rows": [{"size": "M", "chest": 101}],
    }
    assert body["size_chart_url"] == "https://cdn.example.com/chart.png"
    assert body["manufacturer"]["city"] == "Bengaluru"
    assert body["manufacturer"]["inherited"] is False
    assert "email" not in body["manufacturer"] and "phone" not in body["manufacturer"]
    row = conn.execute("SELECT updated_at FROM products WHERE id = %s", [product_id]).fetchone()
    assert row is not None
    assert response.headers["ETag"] == product_etag(product_id, row[0])


async def test_products_get_malformed_composition_reads_none(
    api: httpx.AsyncClient, products_world: World
) -> None:
    from . import factories as f

    acme = products_world.tenant("acme")
    product_id = f.product(
        products_world.conn,
        acme,
        attributes={"fiber_composition": f.fact(value="x", normalized={"not": "a list"})},
    )
    response = await _get(api, products_world, product_id)
    assert response.status_code == 200, response.text
    assert response.json()["fiber_composition"] is None


async def test_colourways_only_own_style(api: httpx.AsyncClient, products_world: World) -> None:
    from . import factories as f

    acme = products_world.tenant("acme")
    conn = products_world.conn
    style = f.product(conn, acme, "style")
    other = f.product(conn, acme, "style")
    mine = f.colourway(conn, acme, style)
    f.colourway(conn, acme, other)
    body = (await _get(api, products_world, style)).json()
    assert [c["id"] for c in body["colourways"]] == [str(mine)]
    standalone = f.product(conn, acme)
    assert (await _get(api, products_world, standalone)).json()["colourways"] == []


async def test_manufacturer_inherited_from_brand_default(
    api: httpx.AsyncClient, products_world: World
) -> None:
    from . import factories as f

    acme = products_world.tenant("acme")
    conn = products_world.conn
    default = f.party(conn, acme, legal_name="Brand Default Pvt Ltd")
    conn.execute("DELETE FROM brand_profiles WHERE tenant_id = %s", [acme])
    f.brand_profile(
        conn,
        acme,
        default_manufacturer_party_id=default,
        provenance={"manufacturer": f.fact(value=None)},
    )
    plain = f.product(conn, acme)
    body = (await _get(api, products_world, plain)).json()
    assert body["manufacturer"]["legal_name"] == "Brand Default Pvt Ltd"
    assert body["manufacturer"]["inherited"] is True
