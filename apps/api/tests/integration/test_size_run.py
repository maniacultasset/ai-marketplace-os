"""V1.1: `POST /products/{id}/size-run` as app_rw through the real app. The seller's saved size
chart becomes size variants, only for the sizes the seller picked; nothing is made silently."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .family_support import family
from .products_support import PRODUCTS, bad_bearer, bearer_for, site_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

CHART = {
    "unit": "cm",
    "basis": "garment",
    "rows": [
        {"size": "S", "chest": 96},
        {"size": "M", "chest": 100},
        {"size": "L", "chest": 104},
        {"size": "3XL", "chest": 116},
    ],
}


def _op(world: World, email: str = "operator@acme.test") -> dict[str, str]:
    return bearer_for(world, email)


def _url(product_id: Any) -> str:
    return f"{PRODUCTS}/{product_id}/size-run"


async def _post(
    api: httpx.AsyncClient,
    world: World,
    product_id: Any,
    sizes: Any,
    *,
    key: str | None = None,
    email: str = "operator@acme.test",
) -> httpx.Response:
    headers = {**_op(world, email), "Idempotency-Key": key or str(uuid.uuid4())}
    return await api.post(_url(product_id), json={"sizes": sizes}, headers=headers)


async def _product(
    api: httpx.AsyncClient, world: World, sku: str, *, chart: Any = CHART, **body: Any
) -> str:
    response = await api.post(PRODUCTS, json=site_body(sku, **body), headers=_op(world))
    assert response.status_code == 201, response.text
    product_id: str = response.json()["id"]
    if chart is not None:
        got = await api.get(f"{PRODUCTS}/{product_id}", headers=_op(world))
        saved = await api.patch(
            f"{PRODUCTS}/{product_id}",
            json={"size_chart": chart},
            headers={**_op(world), "If-Match": got.headers["ETag"]},
        )
        assert saved.status_code == 200, saved.text
    return product_id


def _children(world: World, style: Any) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    rows = world.conn.execute(
        "SELECT sku, variant_attributes, attributes -> 'size' FROM products "
        "WHERE parent_product_id = %s AND deleted_at IS NULL ORDER BY sku",
        [style],
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _style(world: World, product_id: Any) -> tuple[str | None, list[str] | None]:
    row = world.conn.execute(
        "SELECT variation_theme, size_run FROM products WHERE id = %s", [product_id]
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def _code(response: httpx.Response) -> str:
    code: str = response.json()["error"]["type"]
    return code


def _field(response: httpx.Response) -> str:
    """The refused body path of a 422 (`sizes[1]`)."""
    error = response.json()["error"]
    loc = error["details"][0]["loc"][1:]
    return "".join(
        f"[{p}]" if isinstance(p, int) else ("." if i else "") + p for i, p in enumerate(loc)
    )


# ---- success ----


@pytest.mark.endpoint("products_size_run_create", case="success")
async def test_standalone_becomes_a_size_style(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _product(api, products_world, "KORA-SHIRT")
    response = await _post(api, products_world, pid, ["m", "S", "3xl"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["variation_theme"] == "size"
    assert body["size_run"] == ["s", "m", "3xl"]  # the chart's order, not the ask's
    assert [(c["sku"], c["size"]) for c in body["created"]] == [
        ("KORA-SHIRT-S", "S"),
        ("KORA-SHIRT-M", "M"),
        ("KORA-SHIRT-3XL", "3XL"),
    ]
    assert _style(products_world, pid) == ("size", ["s", "m", "3xl"])
    children = _children(products_world, pid)
    assert [(sku, axes) for sku, axes, _ in children] == [
        ("KORA-SHIRT-3XL", {"size": "3xl"}),
        ("KORA-SHIRT-M", {"size": "m"}),
        ("KORA-SHIRT-S", {"size": "s"}),
    ]
    for _, _, size in children:
        assert size["source"] == "seller" and size["normalized"] == size["value"].lower()
    cells = products_world.conn.execute(
        "SELECT size_key, availability FROM variant_matrix_cells WHERE style_product_id = %s "
        "ORDER BY size_key",
        [pid],
    ).fetchall()
    assert [tuple(c) for c in cells] == [
        ("3xl", "available"),
        ("m", "available"),
        ("s", "available"),
    ]
    audit = products_world.conn.execute(
        "SELECT reason FROM audit_log WHERE entity_id = %s AND action = 'product.size_run_created'",
        [pid],
    ).fetchall()
    assert [a[0] for a in audit] == ["skus:KORA-SHIRT-S,KORA-SHIRT-M,KORA-SHIRT-3XL"]
    read = await api.get(f"{PRODUCTS}/{pid}", headers=_op(products_world))
    assert (read.json()["variation_theme"], read.json()["size_run"]) == ("size", ["s", "m", "3xl"])


async def test_repeat_and_later_sizes(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _product(api, products_world, "KORA-TEE")
    key = str(uuid.uuid4())
    first = await _post(api, products_world, pid, ["S", "M"], key=key)
    assert first.status_code == 201, first.text
    replay = await _post(api, products_world, pid, ["S", "M"], key=key)
    assert (replay.status_code, replay.json()) == (201, first.json())
    again = await _post(api, products_world, pid, ["M", "S"])
    assert again.status_code == 200, again.text
    assert again.json()["created"] == [] and again.json()["size_run"] == ["s", "m"]
    more = await _post(api, products_world, pid, ["S", "L"])
    assert more.status_code == 201, more.text
    assert [c["sku"] for c in more.json()["created"]] == ["KORA-TEE-L"]
    assert _style(products_world, pid) == ("size", ["s", "m", "l"])
    assert len(_children(products_world, pid)) == 3


async def test_exemption_moves_to_every_size(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _product(api, products_world, "KORA-EXEMPT", identifier_exemption=True)
    response = await _post(api, products_world, pid, ["S", "M"])
    assert response.status_code == 201, response.text
    rows = products_world.conn.execute(
        "SELECT parent_product_id IS NULL, identifier_status FROM products "
        "WHERE id = %s OR parent_product_id = %s",
        [pid, pid],
    ).fetchall()
    assert sorted((bool(r[0]), r[1]) for r in rows) == [
        (False, "exempt"),
        (False, "exempt"),
        (True, None),
    ]
    read = await api.get(f"{PRODUCTS}/{pid}", headers=_op(products_world))
    assert read.json()["identifier_exemption"] is True
    later = await _post(api, products_world, pid, ["L"])
    assert later.status_code == 201, later.text
    added = products_world.conn.execute(
        "SELECT identifier_status FROM products WHERE sku = 'KORA-EXEMPT-L'"
    ).fetchone()
    assert added is not None and added[0] == "exempt"
    read = await api.get(f"{PRODUCTS}/{pid}", headers=_op(products_world))
    assert read.json()["identifier_exemption"] is True


# ---- refusals ----


@pytest.mark.endpoint("products_size_run_create", case="validation")
async def test_sizes_must_come_from_the_saved_chart(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _product(api, products_world, "KORA-VAL")
    response = await _post(api, products_world, pid, ["S", "XL"])
    assert (response.status_code, _code(response), _field(response)) == (
        422,
        "VALIDATION_FAILED",
        "sizes[1]",
    )
    response = await _post(api, products_world, pid, ["S", "s"])
    assert (response.status_code, _field(response)) == (422, "sizes[1]")
    for bad in ([], [""], "S", [1], ["x" * 21]):
        assert (await _post(api, products_world, pid, bad)).status_code == 422
    bare = await _product(api, products_world, "KORA-NOCHART", chart=None)
    response = await _post(api, products_world, bare, ["S"])
    assert (response.status_code, _field(response)) == (422, "size_chart")
    assert _style(products_world, pid) == (None, None)
    assert _children(products_world, pid) == []


async def test_a_style_holds_at_most_40_sizes(
    api: httpx.AsyncClient, products_world: World
) -> None:
    first = {
        "unit": "cm",
        "basis": "garment",
        "rows": [{"size": f"A{i}", "chest": 80 + i} for i in range(30)],
    }
    pid = await _product(api, products_world, "KORA-CAP", chart=first)
    made = await _post(api, products_world, pid, [f"A{i}" for i in range(30)])
    assert made.status_code == 201, made.text
    second = {
        "unit": "cm",
        "basis": "garment",
        "rows": [{"size": f"B{i}", "chest": 80 + i} for i in range(11)],
    }
    got = await api.get(f"{PRODUCTS}/{pid}", headers=_op(products_world))
    saved = await api.patch(
        f"{PRODUCTS}/{pid}",
        json={"size_chart": second},
        headers={**_op(products_world), "If-Match": got.headers["ETag"]},
    )
    assert saved.status_code == 200, saved.text
    response = await _post(api, products_world, pid, [f"B{i}" for i in range(11)])
    assert (response.status_code, _code(response), _field(response)) == (
        422,
        "VALIDATION_FAILED",
        "sizes",
    )
    assert len(_children(products_world, pid)) == 30


async def test_a_variant_or_a_colour_style_is_refused(
    api: httpx.AsyncClient, products_world: World
) -> None:
    style, children = family(
        products_world.conn, products_world.tenant("acme"), "FAM-SR", sizes=("s",), colours=("red",)
    )
    child = next(iter(children.values()))
    response = await _post(api, products_world, child, ["S"])
    assert (response.status_code, _code(response)) == (422, "FACT_LEVEL_INVALID")
    response = await _post(api, products_world, style, ["S"])
    assert (response.status_code, _code(response)) == (422, "VARIATION_INVALID")


async def test_a_barcode_belongs_to_one_size(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _product(
        api,
        products_world,
        "KORA-EAN",
        product_identifier={"type": "ean", "value": "4006381333931"},
    )
    response = await _post(api, products_world, pid, ["S"])
    assert (response.status_code, _code(response), _field(response)) == (
        422,
        "VARIATION_INVALID",
        "product_identifier",
    )
    assert _style(products_world, pid) == (None, None)


@pytest.mark.endpoint("products_size_run_create", case="conflict")
async def test_taken_sku_and_reused_key_create_nothing(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _product(api, products_world, "KORA-DUP-M", chart=None)
    pid = await _product(api, products_world, "KORA-DUP")
    response = await _post(api, products_world, pid, ["S", "M"])
    assert (response.status_code, _code(response)) == (409, "SKU_CONFLICT")
    assert _style(products_world, pid) == (None, None) and _children(products_world, pid) == []
    key = str(uuid.uuid4())
    assert (await _post(api, products_world, pid, ["S"], key=key)).status_code == 201
    reused = await _post(api, products_world, pid, ["L"], key=key)
    assert (reused.status_code, _code(reused)) == (409, "IDEMPOTENCY_KEY_REUSED")


async def test_a_listing_the_marketplace_has_seen_blocks_it(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _product(api, products_world, "KORA-LIVE")
    tenant = products_world.tenant("acme")
    with products_world.conn.transaction():
        products_world.conn.execute("SET LOCAL session_replication_role = replica")
        # once live, now failed: the marketplace has seen it
        f.listing(products_world.conn, tenant, uuid.UUID(pid), first_published_at=datetime.now(UTC))
    response = await _post(api, products_world, pid, ["S"])
    assert (response.status_code, _code(response)) == (409, "INVALID_STATUS_TRANSITION")
    assert _children(products_world, pid) == []


async def test_a_draft_listing_stays_and_the_family_is_made(
    api: httpx.AsyncClient, products_world: World
) -> None:
    pid = await _product(api, products_world, "KORA-DRAFT")
    listing = f.listing(
        products_world.conn, products_world.tenant("acme"), uuid.UUID(pid), category="kurtas"
    )
    response = await _post(api, products_world, pid, ["S", "M"])
    assert response.status_code == 201, response.text
    status = products_world.conn.execute(
        "SELECT status FROM listings WHERE id = %s", [listing]
    ).fetchone()
    assert status is not None and status[0] == "created"


@pytest.mark.endpoint("products_size_run_create", case="wrong_role")
async def test_reviewer_403(api: httpx.AsyncClient, products_world: World) -> None:
    pid = await _product(api, products_world, "KORA-ROLE")
    response = await _post(api, products_world, pid, ["S"], email="reviewer@acme.test")
    assert response.status_code == 403
    assert _children(products_world, pid) == []


@pytest.mark.endpoint("products_size_run_create", case="not_found")
async def test_unknown_product_404(api: httpx.AsyncClient, products_world: World) -> None:
    response = await _post(api, products_world, uuid.uuid4(), ["S"])
    assert (response.status_code, _code(response)) == (404, "NOT_FOUND")


@pytest.mark.endpoint("products_size_run_create", case="wrong_tenant")
async def test_other_tenants_product_reads_as_missing(
    api: httpx.AsyncClient, products_world: World
) -> None:
    theirs = f.product(products_world.conn, products_world.tenant("bharat"))
    foreign = await _post(api, products_world, theirs, ["S"])
    missing = await _post(api, products_world, uuid.uuid4(), ["S"])
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


@pytest.mark.endpoint("products_size_run_create", case="missing_auth")
async def test_missing_auth(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.post(
        _url(uuid.uuid4()), json={"sizes": ["S"]}, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 401


@pytest.mark.endpoint("products_size_run_create", case="invalid_token", variant="expired")
@pytest.mark.endpoint("products_size_run_create", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("products_size_run_create", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_invalid_token(api: httpx.AsyncClient, products_world: World, variant: str) -> None:
    headers = {**bad_bearer(products_world, variant), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(_url(uuid.uuid4()), json={"sizes": ["S"]}, headers=headers)
    assert response.status_code == 401
