"""SI12 tester attacks (AC8, AC9, AC11, AC12): edge inputs on the seller fields, the merge
patch across a style and its variant, and the size-chart confirm with images of the right
product but the wrong slot or state. Real stack: API, service, repository, Postgres."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bearer_for, site_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

DIMS = {"length_cm": "30", "breadth_cm": "20.5", "height_cm": "4"}
CHART = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}


def _op(world: World) -> dict[str, str]:
    return bearer_for(world, "operator@acme.test")


async def _create(api: httpx.AsyncClient, world: World, sku: str, **over: Any) -> str:
    response = await api.post(PRODUCTS, json=site_body(sku, **over), headers=_op(world))
    assert response.status_code == 201, response.text
    product_id: str = response.json()["id"]
    return product_id


async def _patch(
    api: httpx.AsyncClient, world: World, product_id: Any, body: object
) -> httpx.Response:
    got = await api.get(f"{PRODUCTS}/{product_id}", headers=_op(world))
    return await api.patch(
        f"{PRODUCTS}/{product_id}",
        json=body,
        headers={**_op(world), "If-Match": got.headers["ETag"]},
    )


def _row(conn: Conn, product_id: Any) -> dict[str, Any]:
    cur = conn.execute(
        "SELECT hsn_code, country_of_origin, package_length_cm, package_breadth_cm, "
        "package_height_cm, package_weight_g, provenance, attributes, updated_at "
        "FROM products WHERE id = %s",
        [product_id],
    )
    names = [d.name for d in cur.description or ()]
    found = cur.fetchone()
    assert found is not None
    return dict(zip(names, found, strict=True))


def _answer(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    body.pop("correlation_id", None)
    body.get("error_report", {}).pop("request_id", None)
    return body


# ---- seller field bounds on PATCH (AC9) ----


@pytest.mark.parametrize(
    "body",
    [
        {"package": {"weight_g": True}},
        {"package": {"weight_g": 500.5}},
        {"package": {"weight_g": "500"}},
        {"package": {"weight_g": 2_000_001}},
        {"package": {"weight_g": -1}},
        {"package": {**DIMS, "length_cm": "100000"}},
        {"package": {**DIMS, "length_cm": "NaN"}},
        {"package": {**DIMS, "length_cm": "Infinity"}},
        {"package": {**DIMS, "length_cm": "-1"}},
        {"package": {**DIMS, "length_cm": "0"}},
        {"package": {**DIMS, "length_cm": "30.555"}},
        {"package": {**DIMS, "height_cm": None}},
        {"package": {"length_cm": None}},
        {"package": {"depth_cm": 3}},
        {"hsn_code": "\uff16\uff11\uff10\uff19"},  # full-width digits
        {"hsn_code": "61 09"},
        {"hsn_code": "610"},
        {"hsn_code": 6109},
        {"country_of_origin": "\uff29\uff2e"},  # full-width IN
        {"country_of_origin": "I"},
        {"country_of_origin": "1N"},
    ],
)
async def test_patch_refuses_edge_values(
    api: httpx.AsyncClient, products_world: World, body: dict[str, Any]
) -> None:
    pid = await _create(
        api, products_world, "SI12-ATK-B", hsn_code="6109", package={**DIMS, "weight_g": 450}
    )
    before = _row(products_world.conn, pid)
    response = await _patch(api, products_world, pid, body)
    assert response.status_code == 422, response.text
    assert _row(products_world.conn, pid) == before


@pytest.mark.parametrize(
    ("body", "column", "expected"),
    [
        ({"package": {"weight_g": 2_000_000}}, "package_weight_g", 2_000_000),
        ({"package": {"weight_g": 1}}, "package_weight_g", 1),
        (
            {"package": {**DIMS, "length_cm": "99999.99"}},
            "package_length_cm",
            Decimal("99999.99"),
        ),
        ({"package": {**DIMS, "length_cm": 0.01}}, "package_length_cm", Decimal("0.01")),
        ({"hsn_code": "61091000"}, "hsn_code", "61091000"),
        ({"hsn_code": "610910"}, "hsn_code", "610910"),
        ({"country_of_origin": "bD"}, "country_of_origin", "BD"),
    ],
)
async def test_patch_takes_the_bounds(
    api: httpx.AsyncClient,
    products_world: World,
    body: dict[str, Any],
    column: str,
    expected: Any,
) -> None:
    pid = await _create(api, products_world, "SI12-ATK-OK")
    response = await _patch(api, products_world, pid, body)
    assert response.status_code == 200, response.text
    stored = _row(products_world.conn, pid)
    assert stored[column] == expected
    assert all(v["source"] == "seller" for v in stored["provenance"].values() if v)


async def test_package_change_moves_the_etag(api: httpx.AsyncClient, products_world: World) -> None:
    """A package-only change is a real change: a stale If-Match after it is 409."""
    pid = await _create(api, products_world, "SI12-ATK-E", package={**DIMS, "weight_g": 450})
    first = await api.get(f"{PRODUCTS}/{pid}", headers=_op(products_world))
    etag = first.headers["ETag"]
    response = await api.patch(
        f"{PRODUCTS}/{pid}",
        json={"package": {"weight_g": 451}},
        headers={**_op(products_world), "If-Match": etag},
    )
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] != etag
    stale = await api.patch(
        f"{PRODUCTS}/{pid}",
        json={"package": {**DIMS, "length_cm": "31"}},
        headers={**_op(products_world), "If-Match": etag},
    )
    assert stale.status_code == 409, stale.text
    assert _row(products_world.conn, pid)["package_length_cm"] == Decimal("30.00")


# ---- the style and its variant (AC9, AC11) ----


def _family(world: World, **style_columns: Any) -> tuple[Any, Any]:
    acme = world.tenant("acme")
    style = f.product(
        world.conn, acme, "style", variation_theme="size", size_run=["m"], **style_columns
    )
    with world.conn.transaction():
        child = f.product(
            world.conn,
            acme,
            "child",
            parent=style,
            variant_attributes={"size": "m"},
            attributes={"size": f.fact(value="M", normalized="m")},
        )
        f.cell(
            world.conn,
            acme,
            style,
            size_key="m",
            availability="available",
            unavailable_reason=None,
            variant_product_id=child,
        )
    return style, child


async def test_variant_own_weight_with_the_style_dims(
    api: httpx.AsyncClient, products_world: World
) -> None:
    """A variant with its own weight and no dims reads its weight and the style's dims, and each
    group's source comes from the layer that holds it."""
    style, child = _family(products_world)
    response = await _patch(
        api, products_world, style, {"package": {**DIMS, "weight_g": 450}, "hsn_code": "6109"}
    )
    assert response.status_code == 200, response.text
    response = await _patch(api, products_world, child, {"package": {"weight_g": 300}})
    assert response.status_code == 200, response.text
    stored = _row(products_world.conn, child)
    assert stored["package_length_cm"] is None and stored["package_weight_g"] == 300
    read = (await api.get(f"{PRODUCTS}/{child}", headers=_op(products_world))).json()
    assert read["package"] == {
        "length_cm": "30.00",
        "breadth_cm": "20.50",
        "height_cm": "4.00",
        "weight_g": 300,
    }, read["package"]
    assert read["hsn_code"] == "6109"
    sources = read["field_sources"]
    for key in ("package_dimensions", "package_weight", "hsn_code"):
        assert sources[key]["kind"] == "you", (key, sources)
    # the style keeps its own weight
    style_read = (await api.get(f"{PRODUCTS}/{style}", headers=_op(products_world))).json()
    assert style_read["package"]["weight_g"] == 450


async def test_variant_clear_falls_back_to_the_style(
    api: httpx.AsyncClient, products_world: World
) -> None:
    style, child = _family(products_world)
    assert (await _patch(api, products_world, style, {"hsn_code": "6109"})).status_code == 200
    assert (await _patch(api, products_world, child, {"hsn_code": "61091000"})).status_code == 200
    read = (await api.get(f"{PRODUCTS}/{child}", headers=_op(products_world))).json()
    assert read["hsn_code"] == "61091000"
    assert (await _patch(api, products_world, child, {"hsn_code": None})).status_code == 200
    read = (await api.get(f"{PRODUCTS}/{child}", headers=_op(products_world))).json()
    assert read["hsn_code"] == "6109"
    assert _row(products_world.conn, child)["hsn_code"] is None


# ---- the size-chart confirm with an image of the right product (AC8) ----


def _image(world: World, product_id: Any, slot: str, **cols: Any) -> str:
    tid = world.tenant("acme")
    image_id = uuid.uuid4()
    f.image(
        world.conn,
        tid,
        product_id,
        id=image_id,
        slot=slot,
        storage_key=f"{tid}/{product_id}/images/{image_id}",
        **cols,
    )
    return str(image_id)


async def test_confirm_refuses_wrong_slot_and_deleted_images_alike(
    api: httpx.AsyncClient, products_world: World
) -> None:
    """The product's own main or secondary image, and its own deleted size chart, answer the
    same 422 as an unknown id, and nothing is stored."""
    pid = await _create(api, products_world, "SI12-ATK-SC")
    main = _image(products_world, pid, "main")
    secondary = _image(products_world, pid, "secondary", role="back")
    deleted = _image(products_world, pid, "size_chart", deleted_at=datetime.now(UTC))
    answers = []
    for image_id in (main, secondary, deleted, str(uuid.uuid4())):
        response = await _patch(
            api, products_world, pid, {"size_chart": CHART, "size_chart_image_id": image_id}
        )
        assert response.status_code == 422, response.text
        answers.append(_answer(response))
    assert all(a == answers[0] for a in answers), answers
    assert answers[0]["details"][0]["loc"][-1] == "size_chart_image_id"
    assert "size_measurements" not in (_row(products_world.conn, pid)["attributes"] or {})


async def test_confirm_with_null_image_id_stores_without_evidence(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _create(api, products_world, "SI12-ATK-SN")
    response = await _patch(
        api, products_world, pid, {"size_chart": CHART, "size_chart_image_id": None}
    )
    assert response.status_code == 200, response.text
    fact = _row(products_world.conn, pid)["attributes"]["size_measurements"]
    assert fact["source"] == "seller" and not fact.get("evidence_ref")
    read = (await api.get(f"{PRODUCTS}/{pid}", headers=_op(products_world))).json()
    assert read["field_sources"]["size_measurements"]["kind"] == "you"


async def test_confirm_then_chart_null_clears_the_source(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _create(api, products_world, "SI12-ATK-SX")
    chart = _image(products_world, pid, "size_chart")
    response = await _patch(
        api, products_world, pid, {"size_chart": CHART, "size_chart_image_id": chart}
    )
    assert response.status_code == 200, response.text
    read = (await api.get(f"{PRODUCTS}/{pid}", headers=_op(products_world))).json()
    assert read["field_sources"]["size_measurements"] == {
        "kind": "size_chart",
        "image_id": chart,
        "confidence": None,
    }
    response = await _patch(api, products_world, pid, {"size_chart": None})
    assert response.status_code == 200, response.text
    read = (await api.get(f"{PRODUCTS}/{pid}", headers=_op(products_world))).json()
    assert "size_measurements" not in read["field_sources"]


# ---- requirements (AC12) ----


@pytest.mark.parametrize("category", ["TSHIRTS", "t shirts", "a" * 200, "../etc", "%00"])
async def test_requirements_refuse_odd_slugs(
    api: httpx.AsyncClient, products_world: World, category: str
) -> None:
    response = await api.get(
        "/api/v1/catalog/requirements", params={"category": category}, headers=_op(products_world)
    )
    assert response.status_code == 422, response.text


async def test_requirements_need_the_category(
    api: httpx.AsyncClient, products_world: World
) -> None:
    response = await api.get("/api/v1/catalog/requirements", headers=_op(products_world))
    assert response.status_code == 422, response.text


async def test_requirements_entries_are_sorted_unique_keys(
    api: httpx.AsyncClient, products_world: World
) -> None:
    response = await api.get(
        "/api/v1/catalog/requirements",
        params={"category": "tshirts"},
        headers=bearer_for(products_world, "reviewer@acme.test"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    rows = products_world.conn.execute("SELECT key FROM attribute_definitions").fetchall()
    keys = {r[0] for r in rows}
    for marketplace in ("amazon", "flipkart"):
        entries = body[marketplace]
        assert entries == sorted(set(entries)), entries
        assert set(entries) <= keys, set(entries) - keys
