"""SI12 T14 (AC8, AC10, AC11): `field_sources` and `suggestions` on the product detail read;
lists carry the empty defaults, and a read writes nothing."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bearer_for, site_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

IMPORTED = {"source": "seller", "captured_via": "import", "updated_at": "2026-11-01T00:00:00Z"}


def _imported(*keys: str) -> dict[str, Any]:
    """Provenance with title and category from the form and `keys` from the CSV import."""
    return {"title": f.fact(value=None), "category": f.fact(value=None)} | dict.fromkeys(
        keys, IMPORTED
    )


CHART = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}


def _op(world: World) -> dict[str, str]:
    return bearer_for(world, "operator@acme.test")


async def _get(api: httpx.AsyncClient, world: World, product_id: Any) -> dict[str, Any]:
    response = await api.get(f"{PRODUCTS}/{product_id}", headers=_op(world))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _patch(
    api: httpx.AsyncClient, world: World, product_id: Any, body: object
) -> httpx.Response:
    got = await api.get(f"{PRODUCTS}/{product_id}", headers=_op(world))
    return await api.patch(
        f"{PRODUCTS}/{product_id}",
        json=body,
        headers={**_op(world), "If-Match": got.headers["ETag"]},
    )


def _row(conn: Conn, product_id: Any) -> tuple[Any, ...]:
    found = conn.execute(
        "SELECT attributes, provenance, updated_at FROM products WHERE id = %s", [product_id]
    ).fetchone()
    assert found is not None
    return tuple(found)


async def test_imported_value_is_sheet(api: httpx.AsyncClient, products_world: World) -> None:
    acme = products_world.tenant("acme")
    pid = f.product(
        products_world.conn,
        acme,
        category="tshirts",
        mrp=Decimal("999"),
        hsn_code="6109",
        provenance=_imported("mrp", "hsn_code"),
        attributes={"fabric_type": f.fact(captured_via="import", value="cotton")},
    )
    sources = (await _get(api, products_world, pid))["field_sources"]
    assert sources["mrp"] == {"kind": "sheet", "image_id": None, "confidence": None}
    assert sources["hsn_code"]["kind"] == "sheet"
    assert sources["fabric_type"]["kind"] == "sheet"


async def test_api_value_is_you_and_hsn_suggestion(
    api: httpx.AsyncClient, products_world: World
) -> None:
    response = await api.post(
        PRODUCTS, json=site_body("SI12-FS-2", category="tshirts"), headers=_op(products_world)
    )
    assert response.status_code == 201, response.text
    pid = response.json()["id"]
    body = await _get(api, products_world, pid)
    assert body["suggestions"] == {"hsn_code": {"value": "6109", "basis": "category"}}
    assert body["field_sources"]["color"]["kind"] == "you"
    assert body["field_sources"]["category"]["kind"] == "you"
    assert "hsn_code" not in body["field_sources"]
    response = await _patch(api, products_world, pid, {"hsn_code": "61091000"})
    assert response.status_code == 200, response.text
    assert response.json()["suggestions"] == {"hsn_code": None}
    assert response.json()["field_sources"]["hsn_code"]["kind"] == "you"


async def test_unseeded_category_no_suggestion(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = f.product(products_world.conn, products_world.tenant("acme"), category="kurtas")
    assert (await _get(api, products_world, pid))["suggestions"] == {"hsn_code": None}


async def test_photo_value_then_confirmed(api: httpx.AsyncClient, products_world: World) -> None:
    acme = products_world.tenant("acme")
    image = uuid.uuid4()
    color = f.fact("ai", "agent", value="navy", evidence_ref=f"image:{image}", confidence=0.82)
    pid = f.product(products_world.conn, acme, category="tshirts", attributes={"color": color})
    sources = (await _get(api, products_world, pid))["field_sources"]
    assert sources["color"] == {"kind": "photo", "image_id": str(image), "confidence": 0.82}
    response = await _patch(api, products_world, pid, {"confirm_attributes": ["color"]})
    assert response.status_code == 200, response.text
    assert response.json()["field_sources"]["color"] == {
        "kind": "confirmed",
        "image_id": str(image),
        "confidence": None,
    }


async def test_brand_defaults_are_brand(api: httpx.AsyncClient, products_world: World) -> None:
    acme = products_world.tenant("acme")
    f.brand_manufacturer(products_world.conn, acme, country="IN")
    pid = f.product(products_world.conn, acme, category="tshirts")
    body = await _get(api, products_world, pid)
    assert body["field_sources"]["manufacturer"]["kind"] == "brand"
    assert body["field_sources"]["country_of_origin"]["kind"] == "brand"
    assert body["country_of_origin"] == "IN"


async def test_variant_reads_the_style_sources(
    api: httpx.AsyncClient, products_world: World
) -> None:
    acme = products_world.tenant("acme")
    style = f.product(
        products_world.conn,
        acme,
        "style",
        variation_theme="size",
        size_run=["m"],
        hsn_code="6204",
        provenance=_imported("hsn_code"),
    )
    with products_world.conn.transaction():
        child = f.product(
            products_world.conn,
            acme,
            "child",
            parent=style,
            variant_attributes={"size": "m"},
            attributes={"size": f.fact(value="M", normalized="m")},
        )
        f.cell(
            products_world.conn,
            acme,
            style,
            size_key="m",
            availability="available",
            unavailable_reason=None,
            variant_product_id=child,
        )
    sources = (await _get(api, products_world, child))["field_sources"]
    assert sources["hsn_code"]["kind"] == "sheet"


async def test_size_chart_source(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.post(PRODUCTS, json=site_body("SI12-FS-7"), headers=_op(products_world))
    pid = response.json()["id"]
    acme = products_world.tenant("acme")
    image = uuid.uuid4()
    f.image(
        products_world.conn,
        acme,
        pid,
        id=image,
        slot="size_chart",
        storage_key=f"{acme}/{pid}/images/{image}",
    )
    response = await _patch(
        api, products_world, pid, {"size_chart": CHART, "size_chart_image_id": str(image)}
    )
    assert response.status_code == 200, response.text
    assert response.json()["field_sources"]["size_measurements"] == {
        "kind": "size_chart",
        "image_id": str(image),
        "confidence": None,
    }
    response = await _patch(api, products_world, pid, {"size_chart": CHART})
    assert response.json()["field_sources"]["size_measurements"]["kind"] == "you"


async def test_list_has_the_empty_defaults_and_reads_write_nothing(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = f.product(
        products_world.conn,
        products_world.tenant("acme"),
        category="tshirts",
        mrp=Decimal("999"),
        provenance=_imported("mrp"),
    )
    before = _row(products_world.conn, pid)
    await _get(api, products_world, pid)
    await _get(api, products_world, pid)
    listed = await api.get(f"{PRODUCTS}?limit=100", headers=_op(products_world))
    item = next(p for p in listed.json()["data"] if p["id"] == str(pid))
    assert item["field_sources"] == {}
    assert item["suggestions"] == {"hsn_code": None}
    assert item["size_chart_suggestion"] is None
    assert _row(products_world.conn, pid) == before
