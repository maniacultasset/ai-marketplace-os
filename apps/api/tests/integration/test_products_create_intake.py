"""SI1 T11 (AC1-3, AC6): `POST /api/v1/products` with the intake fields. Run in the Docker batch
(`make api-tests-integration`)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from .auth_support import World, audit_rows
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bearer_for, has_phone, site_body, wire_chart

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

PARTY: dict[str, Any] = {
    "legal_name": "Acme Garments Pvt Ltd",
    "address_line1": "12 MG Road",
    "address_line2": "Floor 2",
    "city": "Bengaluru",
    "state_code": "KA",
    "postal_code": "560001",
    "gstin": "29ABCDE1234F1Z5",
    "email": "care@acme.example",
    "phone": "+91 80 1234 5678",
}
CHART: dict[str, Any] = {
    "unit": "cm",
    "basis": "garment",
    "rows": [{"size": "S", "chest": 96}, {"size": "M", "chest": 101.5, "length": 72}],
}


def _op(world: World) -> dict[str, str]:
    return bearer_for(world, "operator@acme.test")


def _full(sku: str, **over: Any) -> dict[str, Any]:
    body = site_body(
        sku,
        style_number="ST-9",
        mrp="1299.00",
        selling_price="999",
        manufacturer=PARTY,
        size_chart=CHART,
        attributes={
            "color": "navy blue",
            "fabric_composition": "60% Cotton, 40% Polyester",
            "size_chart_url": "https://cdn.example.com/chart.png",
        },
    )
    body.update(over)
    return body


async def test_create_every_field_reads_back(api: httpx.AsyncClient, products_world: World) -> None:
    created = await api.post(PRODUCTS, json=_full("SI1-ALL"), headers=_op(products_world))
    assert created.status_code == 201, created.text
    assert created.headers["ETag"]
    out = created.json()
    assert (out["style_number"], out["mrp"], out["selling_price"]) == ("ST-9", "1299.00", "999.00")
    assert out["fiber_composition"] == [
        {"fibre": "cotton", "percent": 60},
        {"fibre": "polyester", "percent": 40},
    ]
    assert out["size_chart"] == wire_chart(CHART)
    assert out["size_chart_url"] == "https://cdn.example.com/chart.png"
    assert out["manufacturer"]["legal_name"] == "Acme Garments Pvt Ltd"
    assert out["manufacturer"]["inherited"] is False
    got = await api.get(f"{PRODUCTS}/{out['id']}", headers=_op(products_world))
    assert got.status_code == 200
    assert got.json() == out
    assert got.headers["ETag"] == created.headers["ETag"]


async def test_fabric_composition_alias_stored_canonical(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await api.post(PRODUCTS, json=_full("SI1-ALIAS"), headers=_op(products_world))
    assert created.status_code == 201, created.text
    row = auth_db.execute(
        "SELECT attributes FROM products WHERE id = %s", [created.json()["id"]]
    ).fetchone()
    assert row is not None
    attributes: Any = row[0]
    assert "fabric_composition" not in attributes
    assert attributes["fiber_composition"]["value"] == "60% Cotton, 40% Polyester"


@pytest.mark.parametrize(
    "text", ["59% Cotton, 40% Poly", "101% Cotton", "60% a, 40% A", "0% a, 100% b", "60% , 40% b"]
)
async def test_bad_composition_422(
    api: httpx.AsyncClient, products_world: World, text: str
) -> None:
    body = site_body("SI1-BADCOMP", attributes={"fabric_composition": text})
    response = await api.post(PRODUCTS, json=body, headers=_op(products_world))
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["type"] == "VALIDATION_FAILED"
    assert error["details"][0]["loc"] == ["body", "attributes", "fiber_composition"]


async def test_selling_price_above_mrp_422(api: httpx.AsyncClient, products_world: World) -> None:
    body = site_body("SI1-PRICE", mrp="999.00", selling_price="999.01")
    response = await api.post(PRODUCTS, json=body, headers=_op(products_world))
    assert response.status_code == 422, response.text
    assert "selling_price" in response.text


async def test_money_zero_422(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.post(
        PRODUCTS, json=site_body("SI1-ZERO", mrp="0"), headers=_op(products_world)
    )
    assert response.status_code == 422
    assert "mrp" in response.text


async def test_style_number_nul_422(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.post(
        PRODUCTS, json=site_body("SI1-NUL", style_number="ST\u00009"), headers=_op(products_world)
    )
    assert response.status_code == 422


async def test_duplicate_sku_creates_no_party(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    first = await api.post(PRODUCTS, json=_full("SI1-DUP"), headers=_op(products_world))
    assert first.status_code == 201
    other = {**PARTY, "legal_name": "Second Maker Pvt Ltd"}
    again = await api.post(
        PRODUCTS, json=_full("SI1-DUP", manufacturer=other), headers=_op(products_world)
    )
    assert again.status_code == 409
    assert again.json()["error"]["type"] == "SKU_CONFLICT"
    left = auth_db.execute(
        "SELECT count(*) FROM business_parties WHERE legal_name = 'Second Maker Pvt Ltd'"
    ).fetchone()
    assert left == (0,)


async def test_audit_row_has_no_personal_data(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await api.post(PRODUCTS, json=_full("SI1-AUDIT"), headers=_op(products_world))
    assert created.status_code == 201
    rows = [r for r in audit_rows(auth_db, "product.created") if str(r[5]) == created.json()["id"]]
    assert len(rows) == 1
    after = json.dumps(rows[0][7])
    assert "@" not in after
    assert not has_phone(after) and "phone" not in after
    assert "postal_code" not in after and "560001" not in after
    assert "Acme Garments" not in after


async def test_reviewer_403(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.post(
        PRODUCTS, json=_full("SI1-REV"), headers=bearer_for(products_world, "reviewer@acme.test")
    )
    assert response.status_code == 403


async def test_operator_cannot_rewrite_brand_party(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    """AC6 (decision 16): a product route links a matching party and leaves its row alone."""
    seeded = await api.post(PRODUCTS, json=_full("SI1-AC6-A"), headers=_op(products_world))
    assert seeded.status_code == 201, seeded.text
    tenant = products_world.tenant("acme")
    query = "SELECT * FROM business_parties WHERE legal_name = %s AND tenant_id = %s"
    before = auth_db.execute(query, [PARTY["legal_name"], tenant]).fetchall()
    assert len(before) == 1
    moved = {**PARTY, "city": "Mysuru", "state_code": "TN", "gstin": "33ABCDE1234F1Z5"}
    created = await api.post(
        PRODUCTS, json=_full("SI1-AC6-B", manufacturer=moved), headers=_op(products_world)
    )
    assert created.status_code == 201, created.text
    assert auth_db.execute(query, [PARTY["legal_name"], tenant]).fetchall() == before
    assert created.json()["manufacturer"]["id"] == seeded.json()["manufacturer"]["id"]
    assert created.json()["manufacturer"]["city"] == "Bengaluru"
