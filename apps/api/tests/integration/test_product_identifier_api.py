"""SI5 T10, T11 (AC15, AC16): product barcodes and the GTIN exemption through `products_create`,
`products_update`, `products_get` and `products_list`, as app_rw through the real app."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .auth_support import World, audit_rows
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bearer_for, site_body, statements

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

EAN = {"type": "ean", "value": "4006381333931"}
UPC = {"type": "upc", "value": "036000291452"}


def _op(world: World, email: str = "operator@acme.test") -> dict[str, str]:
    return bearer_for(world, email)


async def _create(
    api: httpx.AsyncClient,
    world: World,
    sku: str,
    *,
    email: str = "operator@acme.test",
    **over: Any,
) -> httpx.Response:
    return await api.post(PRODUCTS, json=site_body(sku, **over), headers=_op(world, email))


async def _get(api: httpx.AsyncClient, world: World, product_id: Any) -> httpx.Response:
    return await api.get(f"{PRODUCTS}/{product_id}", headers=_op(world))


async def _patch(
    api: httpx.AsyncClient, world: World, product_id: Any, body: Any, *, etag: str | None = None
) -> httpx.Response:
    if etag is None:
        etag = (await _get(api, world, product_id)).headers["ETag"]
    return await api.patch(
        f"{PRODUCTS}/{product_id}", json=body, headers={**_op(world), "If-Match": etag}
    )


def _columns(conn: Conn, product_id: Any) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT identifier_type, identifier_value, identifier_status, "
        "provenance -> 'identifier' ->> 'source' FROM products WHERE id = %s",
        [product_id],
    ).fetchone()
    assert row is not None
    return tuple(row)


def _loc(response: httpx.Response) -> list[Any]:
    return list(response.json()["error"]["details"][0]["loc"])


# ---- AC15: create, patch, read ----


async def test_create_with_an_ean(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await _create(api, products_world, "SI5-EAN-1", product_identifier=EAN)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["product_identifier"] == EAN and body["identifier_exemption"] is False
    assert _columns(auth_db, body["id"]) == ("ean", EAN["value"], "provided", "seller")
    got = (await _get(api, products_world, body["id"])).json()
    assert got["product_identifier"] == EAN
    listed = (await api.get(PRODUCTS, headers=_op(products_world))).json()["data"]
    assert [p["product_identifier"] for p in listed if p["id"] == body["id"]] == [EAN]
    (audit,) = [r for r in audit_rows(auth_db, "product.created") if str(r[5]) == body["id"]]
    assert "identifier" in audit[7]["provenance"]
    assert EAN["value"] not in json.dumps(audit[7])


async def test_patch_an_upc_then_the_same_again(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    pid = (await _create(api, products_world, "SI5-UPC-1")).json()["id"]
    first = await _patch(api, products_world, pid, {"product_identifier": UPC})
    assert first.status_code == 200, first.text
    assert first.json()["product_identifier"] == UPC
    etag = first.headers["ETag"]
    again = await _patch(
        api, products_world, pid, {"product_identifier": UPC}, etag='"' + "0" * 36 + '"'
    )
    assert again.status_code == 200 and again.headers["ETag"] == etag
    audits = [r for r in audit_rows(auth_db, "product.fact_updated") if str(r[5]) == pid]
    assert len(audits) == 1
    assert "identifier" in audits[0][7]["provenance"]
    cleared = await _patch(api, products_world, pid, {"product_identifier": None})
    assert cleared.json()["product_identifier"] is None
    assert _columns(auth_db, pid) == (None, None, None, None)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "ean", "value": "4006381333932"},  # check digit
        {"type": "upc", "value": "4006381333931"},  # 13 digits under upc
        {"type": "ean", "value": "036000291452"},  # 12 digits under ean
    ],
)
async def test_bad_barcodes_are_422_at_the_value(
    api: httpx.AsyncClient, products_world: World, bad: dict[str, str]
) -> None:
    created = await _create(
        api, products_world, f"SI5-BAD-{bad['value'][-3:]}", product_identifier=bad
    )
    assert created.status_code == 422, created.text
    assert _loc(created) == ["body", "product_identifier", "value"]
    pid = (await _create(api, products_world, f"SI5-OK-{bad['type']}{bad['value'][-2:]}")).json()[
        "id"
    ]
    patched = await _patch(api, products_world, pid, {"product_identifier": bad})
    assert patched.status_code == 422 and _loc(patched) == ["body", "product_identifier", "value"]


async def test_a_duplicate_in_the_tenant_is_422(
    api: httpx.AsyncClient, products_world: World
) -> None:
    assert (
        await _create(api, products_world, "SI5-DUP-1", product_identifier=EAN)
    ).status_code == 201
    dup = await _create(api, products_world, "SI5-DUP-2", product_identifier=EAN)
    assert dup.status_code == 422 and _loc(dup) == ["body", "product_identifier", "value"]
    # the same digits as a GTIN-14 with a leading zero are the same barcode
    padded = {"type": "gtin", "value": "0" + EAN["value"]}
    other = (await _create(api, products_world, "SI5-DUP-3")).json()["id"]
    patched = await _patch(api, products_world, other, {"product_identifier": padded})
    assert patched.status_code == 422 and _loc(patched) == ["body", "product_identifier", "value"]


async def test_the_same_ean_in_another_tenant_is_allowed(
    api: httpx.AsyncClient, products_world: World
) -> None:
    acme = await _create(api, products_world, "SI5-ISO-1", product_identifier=EAN)
    bharat = await _create(
        api, products_world, "SI5-ISO-1", email="operator@bharat.test", product_identifier=EAN
    )
    assert (acme.status_code, bharat.status_code) == (201, 201)


async def test_both_fields_are_422_at_the_exemption(
    api: httpx.AsyncClient, products_world: World
) -> None:
    both = await _create(
        api, products_world, "SI5-BOTH", product_identifier=EAN, identifier_exemption=True
    )
    assert both.status_code == 422 and _loc(both) == ["body", "identifier_exemption"]


async def test_exemption_on_a_standalone(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    pid = (await _create(api, products_world, "SI5-EX-1", product_identifier=EAN)).json()["id"]
    exempt = await _patch(api, products_world, pid, {"identifier_exemption": True})
    assert exempt.status_code == 200, exempt.text
    assert exempt.json()["identifier_exemption"] is True
    assert exempt.json()["product_identifier"] is None
    assert _columns(auth_db, pid) == (None, None, "exempt", "seller")
    back = await _patch(api, products_world, pid, {"identifier_exemption": False})
    assert back.json()["identifier_exemption"] is False
    assert _columns(auth_db, pid) == (None, None, None, None)
    null = await _patch(api, products_world, pid, {"identifier_exemption": None})
    assert null.status_code == 422


async def _family(world: World, children: int = 1) -> tuple[uuid.UUID, list[uuid.UUID]]:
    acme = world.tenant("acme")
    sizes = [f"s{n}" for n in range(children)]
    run = sizes or ["m"]  # a style keeps a size run even before its variants exist
    style = f.product(world.conn, acme, "style", variation_theme="size", size_run=run)
    kids = []
    for size in sizes:
        with world.conn.transaction():
            child = f.product(
                world.conn,
                acme,
                "child",
                parent=style,
                variant_attributes={"size": size},
                attributes={"size": f.fact(value=size.upper(), normalized=size)},
            )
            f.cell(
                world.conn,
                acme,
                style,
                size_key=size,
                availability="available",
                unavailable_reason=None,
                variant_product_id=child,
            )
        kids.append(child)
    return style, kids


async def test_a_barcode_on_a_style_is_fact_level_invalid(
    api: httpx.AsyncClient, products_world: World
) -> None:
    style, (child,) = await _family(products_world)
    refused = await _patch(api, products_world, style, {"product_identifier": EAN})
    assert refused.status_code == 422
    assert refused.json()["error"]["type"] == "FACT_LEVEL_INVALID"
    ok = await _patch(api, products_world, child, {"product_identifier": EAN})
    assert ok.status_code == 200 and ok.json()["product_identifier"] == EAN


async def test_concurrent_creates_of_one_ean(api: httpx.AsyncClient, products_world: World) -> None:
    first, second = await asyncio.gather(
        _create(api, products_world, "SI5-RACE-1", product_identifier=EAN),
        _create(api, products_world, "SI5-RACE-2", product_identifier=EAN),
    )
    assert sorted([first.status_code, second.status_code]) == [201, 422]


async def test_list_reads_the_style_value_in_one_query_a_page(
    api: httpx.AsyncClient, products_world: World
) -> None:
    for _ in range(2):
        await _family(products_world)
    with statements() as small:
        assert (await api.get(PRODUCTS, headers=_op(products_world))).status_code == 200
    for _ in range(18):
        await _family(products_world)
    with statements() as large:
        assert (await api.get(PRODUCTS, headers=_op(products_world))).status_code == 200
    assert len(large) == len(small)


# ---- part 2 (T11, AC16): a style's exemption fans out ----


async def _exempt_style(
    api: httpx.AsyncClient, world: World, style: Any, value: bool, *, etag: str | None = None
) -> httpx.Response:
    return await _patch(api, world, style, {"identifier_exemption": value}, etag=etag)


async def test_style_exemption_skips_the_variant_with_an_ean(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    style, kids = await _family(products_world, 3)
    assert (
        await _patch(api, products_world, kids[0], {"product_identifier": EAN})
    ).status_code == 200
    response = await _exempt_style(api, products_world, style, True)
    assert response.status_code == 200, response.text
    assert response.json()["identifier_exemption"] is False  # one variant keeps its EAN
    assert _columns(auth_db, kids[0])[:3] == ("ean", EAN["value"], "provided")
    for kid in kids[1:]:
        assert _columns(auth_db, kid) == (None, None, "exempt", "seller")
    audits = [r for r in audit_rows(auth_db, "product.fact_updated") if r[5] in kids[1:]]
    assert len(audits) == 2
    assert all(str(r[5]) != str(style) for r in audit_rows(auth_db, "product.fact_updated"))
    assert (
        await _patch(api, products_world, kids[0], {"identifier_exemption": True})
    ).status_code == 200
    assert (await _get(api, products_world, style)).json()["identifier_exemption"] is True
    cleared = await _exempt_style(api, products_world, style, False)
    assert cleared.json()["identifier_exemption"] is False
    for kid in kids:
        assert _columns(auth_db, kid) == (None, None, None, None)


async def test_false_keeps_a_provided_barcode(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    style, kids = await _family(products_world, 2)
    await _patch(api, products_world, kids[0], {"product_identifier": UPC})
    await _exempt_style(api, products_world, style, True)
    await _exempt_style(api, products_world, style, False)
    assert _columns(auth_db, kids[0])[:3] == ("upc", UPC["value"], "provided")
    assert _columns(auth_db, kids[1]) == (None, None, None, None)


async def test_a_style_without_variants(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    style, _ = await _family(products_world, 0)
    before = auth_db.execute("SELECT count(*) FROM audit_log").fetchone()
    response = await _exempt_style(api, products_world, style, True)
    assert response.status_code == 200 and response.json()["identifier_exemption"] is False
    assert auth_db.execute("SELECT count(*) FROM audit_log").fetchone() == before


async def test_a_stale_etag_writes_nothing(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    style, kids = await _family(products_world, 2)
    response = await _exempt_style(api, products_world, style, True, etag='"' + "0" * 36 + '"')
    assert response.status_code == 409
    for kid in kids:
        assert _columns(auth_db, kid) == (None, None, None, None)


async def test_list_and_get_agree_on_the_style_value(
    api: httpx.AsyncClient, products_world: World
) -> None:
    exempt_style, _ = await _family(products_world, 2)
    await _exempt_style(api, products_world, exempt_style, True)
    mixed_style, mixed = await _family(products_world, 2)
    await _patch(api, products_world, mixed[0], {"product_identifier": EAN})
    await _exempt_style(api, products_world, mixed_style, True)
    empty_style, _ = await _family(products_world, 0)
    listed = {
        p["id"]: p["identifier_exemption"]
        for p in (
            await api.get(PRODUCTS, params={"limit": 50}, headers=_op(products_world))
        ).json()["data"]
    }
    expected = {str(exempt_style): True, str(mixed_style): False, str(empty_style): False}
    for style, value in expected.items():
        assert listed[style] is value, style
        assert (await _get(api, products_world, style)).json()["identifier_exemption"] is value
