"""SI12 T9-T10 (AC9): HSN, package size and weight and country of origin on create, patch and
read, as seller column facts under the dictionary keys."""

from __future__ import annotations

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
COLUMNS = (
    "hsn_code, country_of_origin, package_length_cm, package_breadth_cm, package_height_cm, "
    "package_weight_g, provenance"
)
KEYS = ("hsn_code", "country_of_origin", "package_dimensions", "package_weight")


def _op(world: World, email: str = "operator@acme.test") -> dict[str, str]:
    return bearer_for(world, email)


async def _create(api: httpx.AsyncClient, world: World, sku: str, **over: Any) -> httpx.Response:
    return await api.post(PRODUCTS, json=site_body(sku, **over), headers=_op(world))


async def _patch(
    api: httpx.AsyncClient, world: World, product_id: Any, body: object, email: str | None = None
) -> httpx.Response:
    got = await api.get(f"{PRODUCTS}/{product_id}", headers=_op(world))
    return await api.patch(
        f"{PRODUCTS}/{product_id}",
        json=body,
        headers={**_op(world, email or "operator@acme.test"), "If-Match": got.headers["ETag"]},
    )


def _stored(conn: Conn, product_id: Any) -> dict[str, Any]:
    cur = conn.execute(f"SELECT {COLUMNS} FROM products WHERE id = %s", [product_id])  # noqa: S608
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def _loc(response: httpx.Response) -> list[tuple[Any, ...]]:
    return [tuple(d["loc"]) for d in response.json()["error"].get("details", [])]


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


# ---- create ----


async def test_create_with_all_three(api: httpx.AsyncClient, products_world: World) -> None:
    response = await _create(
        api,
        products_world,
        "SI12-SF-1",
        hsn_code="6109",
        country_of_origin="in",
        package={**DIMS, "weight_g": 450},
    )
    assert response.status_code == 201, response.text
    stored = _stored(products_world.conn, response.json()["id"])
    assert stored["hsn_code"] == "6109"
    assert stored["country_of_origin"] == "IN"
    assert (
        stored["package_length_cm"],
        stored["package_breadth_cm"],
        stored["package_height_cm"],
    ) == (Decimal("30.00"), Decimal("20.50"), Decimal("4.00"))
    assert stored["package_weight_g"] == 450
    for key in KEYS:
        assert stored["provenance"][key]["source"] == "seller", key
        assert stored["provenance"][key]["captured_via"] == "api", key


async def test_create_weight_only(api: httpx.AsyncClient, products_world: World) -> None:
    response = await _create(api, products_world, "SI12-SF-2", package={"weight_g": 300})
    assert response.status_code == 201, response.text
    stored = _stored(products_world.conn, response.json()["id"])
    assert stored["package_weight_g"] == 300 and stored["package_length_cm"] is None
    assert "package_dimensions" not in stored["provenance"]


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"hsn_code": "61091"}, "hsn_code"),
        ({"package": {"length_cm": 1, "breadth_cm": 2}}, "package"),
        ({"package": {"weight_g": 0}}, "package"),
        ({"package": {}}, "package"),
        ({"country_of_origin": "IND"}, "country_of_origin"),
    ],
)
async def test_create_bounds_422(
    api: httpx.AsyncClient, products_world: World, body: dict[str, Any], loc: str
) -> None:
    response = await _create(api, products_world, "SI12-SF-BAD", **body)
    assert response.status_code == 422, response.text
    assert any(loc in path for path in _loc(response)), response.text


# ---- patch ----


async def _standalone(api: httpx.AsyncClient, world: World, sku: str) -> str:
    response = await _create(
        api, world, sku, hsn_code="6109", country_of_origin="IN", package={**DIMS, "weight_g": 450}
    )
    assert response.status_code == 201, response.text
    product_id: str = response.json()["id"]
    return product_id


async def test_patch_each_and_null_clears(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _standalone(api, products_world, "SI12-SF-P1")
    response = await _patch(
        api, products_world, pid, {"hsn_code": "61091000", "country_of_origin": "bd"}
    )
    assert response.status_code == 200, response.text
    stored = _stored(products_world.conn, pid)
    assert (stored["hsn_code"], stored["country_of_origin"]) == ("61091000", "BD")
    response = await _patch(api, products_world, pid, {"hsn_code": None, "country_of_origin": None})
    assert response.status_code == 200, response.text
    stored = _stored(products_world.conn, pid)
    assert stored["hsn_code"] is None and stored["country_of_origin"] is None
    assert "hsn_code" not in stored["provenance"]
    assert "country_of_origin" not in stored["provenance"]


async def test_package_weight_only_keeps_the_dims(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _standalone(api, products_world, "SI12-SF-P2")
    response = await _patch(api, products_world, pid, {"package": {"weight_g": 500}})
    assert response.status_code == 200, response.text
    stored = _stored(products_world.conn, pid)
    assert stored["package_weight_g"] == 500
    assert stored["package_length_cm"] == Decimal("30.00")
    assert "package_dimensions" in stored["provenance"]


async def test_package_weight_null_clears_only_the_weight(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _standalone(api, products_world, "SI12-SF-P3")
    response = await _patch(api, products_world, pid, {"package": {"weight_g": None}})
    assert response.status_code == 200, response.text
    stored = _stored(products_world.conn, pid)
    assert stored["package_weight_g"] is None and "package_weight" not in stored["provenance"]
    assert stored["package_height_cm"] == Decimal("4.00")


async def test_package_dims_null_clears_only_the_dims(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _standalone(api, products_world, "SI12-SF-P4")
    nulls = {"length_cm": None, "breadth_cm": None, "height_cm": None}
    response = await _patch(api, products_world, pid, {"package": nulls})
    assert response.status_code == 200, response.text
    stored = _stored(products_world.conn, pid)
    assert stored["package_length_cm"] is None and stored["package_weight_g"] == 450
    assert "package_dimensions" not in stored["provenance"]


async def test_package_null_clears_all_four(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _standalone(api, products_world, "SI12-SF-P5")
    response = await _patch(api, products_world, pid, {"package": None})
    assert response.status_code == 200, response.text
    stored = _stored(products_world.conn, pid)
    assert all(
        stored[c] is None
        for c in (
            "package_length_cm",
            "package_breadth_cm",
            "package_height_cm",
            "package_weight_g",
        )
    )
    assert not {"package_dimensions", "package_weight"} & set(stored["provenance"])


async def test_package_new_dims_replace_the_group(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _standalone(api, products_world, "SI12-SF-P6")
    dims = {"length_cm": 10, "breadth_cm": 11, "height_cm": 12}
    response = await _patch(api, products_world, pid, {"package": dims})
    assert response.status_code == 200, response.text
    stored = _stored(products_world.conn, pid)
    assert stored["package_breadth_cm"] == Decimal("11.00") and stored["package_weight_g"] == 450


async def test_same_values_change_nothing(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _standalone(api, products_world, "SI12-SF-P7")
    before = _stored(products_world.conn, pid)
    response = await _patch(
        api, products_world, pid, {"hsn_code": "6109", "package": {**DIMS, "weight_g": 450}}
    )
    assert response.status_code == 200, response.text
    assert _stored(products_world.conn, pid) == before


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"hsn_code": "abcd"}, "hsn_code"),
        ({"package": {"length_cm": 1, "breadth_cm": 2}}, "package"),
        ({"package": {"weight_g": 0}}, "package"),
        ({"package": {"length_cm": None, "breadth_cm": None, "height_cm": 3}}, "package"),
    ],
)
async def test_patch_bounds_422(
    api: httpx.AsyncClient, products_world: World, body: dict[str, Any], loc: str
) -> None:
    pid = await _standalone(api, products_world, "SI12-SF-P8")
    response = await _patch(api, products_world, pid, body)
    assert response.status_code == 422, response.text
    assert any(loc in path for path in _loc(response)), response.text


async def test_stale_if_match_409(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _standalone(api, products_world, "SI12-SF-P9")
    got = await api.get(f"{PRODUCTS}/{pid}", headers=_op(products_world))
    first = await api.patch(
        f"{PRODUCTS}/{pid}",
        json={"hsn_code": "6110"},
        headers={**_op(products_world), "If-Match": got.headers["ETag"]},
    )
    assert first.status_code == 200
    stale = await api.patch(
        f"{PRODUCTS}/{pid}",
        json={"hsn_code": "6111"},
        headers={**_op(products_world), "If-Match": got.headers["ETag"]},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["type"] == "STALE_VERSION"


async def test_reviewer_cannot_patch(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _standalone(api, products_world, "SI12-SF-P10")
    response = await _patch(api, products_world, pid, {"hsn_code": "6110"}, "reviewer@acme.test")
    assert response.status_code == 403


async def test_variant_takes_its_own_values(api: httpx.AsyncClient, products_world: World) -> None:
    _, child = _family(products_world)
    response = await _patch(api, products_world, child, {"hsn_code": "6204", "package": DIMS})
    assert response.status_code == 200, response.text
    stored = _stored(products_world.conn, child)
    assert stored["hsn_code"] == "6204" and stored["package_length_cm"] == Decimal("30.00")
