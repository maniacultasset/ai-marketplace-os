"""SI12 T11 (AC8): the seller saves a size chart read from a size-chart photo; the Fact keeps
`source seller` and gains `evidence_ref image:<id>`. A bad image id gets one answer (no oracle)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bearer_for, site_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

CHART = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}


def _op(world: World) -> dict[str, str]:
    return bearer_for(world, "operator@acme.test")


async def _create(api: httpx.AsyncClient, world: World, sku: str) -> str:
    response = await api.post(PRODUCTS, json=site_body(sku), headers=_op(world))
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


def _image(
    world: World, tenant: str, product_id: Any, slot: str = "size_chart", **cols: Any
) -> str:
    tid = world.tenant(tenant)
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


def _chart_fact(conn: Conn, product_id: Any) -> dict[str, Any]:
    row = conn.execute(
        "SELECT attributes -> 'size_measurements' FROM products WHERE id = %s", [product_id]
    ).fetchone()
    assert row is not None and row[0] is not None
    fact: dict[str, Any] = row[0]
    return fact


def _answer(response: httpx.Response) -> dict[str, Any]:
    """The error body without its per-request ids."""
    body: dict[str, Any] = response.json()["error"]
    body.pop("correlation_id", None)
    body["error_report"].pop("request_id", None)
    return body


async def test_confirm_stores_the_evidence(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _create(api, products_world, "SI12-SC-1")
    image = _image(products_world, "acme", pid)
    response = await _patch(
        api, products_world, pid, {"size_chart": CHART, "size_chart_image_id": image}
    )
    assert response.status_code == 200, response.text
    fact = _chart_fact(products_world.conn, pid)
    assert fact["evidence_ref"] == f"image:{image}"
    assert (fact["source"], fact["captured_via"]) == ("seller", "api")
    assert fact["normalized"]["rows"] == [{"size": "M", "chest": 100}]


async def test_chart_without_id_is_stored_as_before(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _create(api, products_world, "SI12-SC-2")
    response = await _patch(api, products_world, pid, {"size_chart": CHART})
    assert response.status_code == 200, response.text
    fact = _chart_fact(products_world.conn, pid)
    assert set(fact) == {"normalized", "source", "captured_via", "updated_at"}
    assert fact["normalized"] == {
        "unit": "cm",
        "basis": "garment",
        "rows": [{"size": "M", "chest": 100}],
    }


async def test_confirm_unchanged_chart_then_resave_without_id(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _create(api, products_world, "SI12-SC-3")
    image = _image(products_world, "acme", pid)
    assert (await _patch(api, products_world, pid, {"size_chart": CHART})).status_code == 200
    response = await _patch(
        api, products_world, pid, {"size_chart": CHART, "size_chart_image_id": image}
    )
    assert response.status_code == 200, response.text
    assert _chart_fact(products_world.conn, pid)["evidence_ref"] == f"image:{image}"
    response = await _patch(api, products_world, pid, {"size_chart": CHART})
    assert response.status_code == 200, response.text
    assert "evidence_ref" not in _chart_fact(products_world.conn, pid)


async def test_image_id_alone_422(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _create(api, products_world, "SI12-SC-4")
    image = _image(products_world, "acme", pid)
    response = await _patch(api, products_world, pid, {"size_chart_image_id": image})
    assert response.status_code == 422
    assert [tuple(d["loc"]) for d in response.json()["error"]["details"]] == [
        ("body", "size_chart_image_id")
    ]


async def test_bad_image_ids_share_one_answer(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _create(api, products_world, "SI12-SC-5")
    other = await _create(api, products_world, "SI12-SC-6")
    rival = f.product(products_world.conn, products_world.tenant("bharat"))
    cases = {
        "foreign": _image(products_world, "bharat", rival),
        "other_product": _image(products_world, "acme", other),
        "deleted": _image(products_world, "acme", pid, deleted_at=datetime.now(UTC)),
        "main": _image(products_world, "acme", pid, slot="main"),
        "unknown": str(uuid.uuid4()),
    }
    answers = []
    for name, image in cases.items():
        response = await _patch(
            api, products_world, pid, {"size_chart": CHART, "size_chart_image_id": image}
        )
        assert response.status_code == 422, (name, response.text)
        answers.append(_answer(response))
    assert all(a == answers[0] for a in answers)
    assert answers[0]["type"] == "VALIDATION_FAILED"
    assert "size_measurements" not in (
        products_world.conn.execute(
            "SELECT attributes FROM products WHERE id = %s", [pid]
        ).fetchone()[
            0
        ]  # type: ignore[index]
    )


async def test_variant_is_fact_level_invalid(api: httpx.AsyncClient, products_world: World) -> None:
    acme = products_world.tenant("acme")
    style = f.product(products_world.conn, acme, "style", variation_theme="size", size_run=["m"])
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
    image = _image(products_world, "acme", style)
    response = await _patch(
        api, products_world, child, {"size_chart": CHART, "size_chart_image_id": image}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "FACT_LEVEL_INVALID"
