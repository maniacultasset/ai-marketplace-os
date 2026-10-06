"""S12 tester attacks on the products routes (round 1): hostile input must never reach
PostgreSQL as a 500, a forged claim must never widen the caller, and a foreign cursor or id
must never surface another tenant's row."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import httpx
import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bearer_for, counts, insert_products, site_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _acme(world: World, role: str = "operator") -> dict[str, str]:
    return bearer_for(world, f"{role}@acme.test")


def _cursor(stamp: str, row_id: str) -> str:
    raw = json.dumps({"t": stamp, "id": row_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _post_raw(api: httpx.AsyncClient, world: World, raw: str) -> Any:
    return api.post(
        PRODUCTS,
        content=raw.encode("utf-8", "surrogatepass"),
        headers={**_acme(world), "Content-Type": "application/json"},
    )


# ---- hostile text: every refusal is a 4xx, never a 500, and writes nothing ----


@pytest.mark.parametrize("field", ["sku", "title", "attr"])
async def test_lone_surrogate_is_refused_not_500(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, field: str
) -> None:
    body = site_body("S12-SUR")
    raw = json.dumps(body)
    if field == "sku":
        raw = raw.replace('"S12-SUR"', '"S12-\\ud800"')
    elif field == "title":
        raw = raw.replace('"Organic Cotton Kurta"', '"Kurta \\udfff"')
    else:
        raw = raw.replace('"navy blue"', '"navy \\ud83d"')
    response = await _post_raw(api, products_world, raw)
    assert response.status_code in (400, 422), response.text
    assert counts(auth_db) == (0, 0)


# NBSP, EM SPACE, IDEOGRAPHIC SPACE, OGHAM, LINE SEP, NEL, MONGOLIAN VS, BOM, ZWSP
_SPACES = [0xA0, 0x2003, 0x3000, 0x1680, 0x2028, 0x85, 0x180E, 0xFEFF, 0x200B]


@pytest.mark.parametrize("space", [chr(c) for c in _SPACES], ids=[f"U+{c:04X}" for c in _SPACES])
async def test_unicode_space_in_sku_never_500(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, space: str
) -> None:
    response = await api.post(
        PRODUCTS, json=site_body(f"S12{space}U"), headers=_acme(products_world)
    )
    assert response.status_code in (201, 422), response.text
    if response.status_code == 201:
        row = auth_db.execute("SELECT sku FROM products").fetchone()
        assert row is not None and row[0] == f"S12{space}U"


@pytest.mark.parametrize(
    "value",
    [
        10**30,
        -(2**63) - 1,
        -0.0,
        1.5e-3,
        "",
        "\u0001\u001f" * 1000,
        "\U0001f600" * 2000,
        True,
    ],
)
async def test_attribute_scalars_round_trip(
    api: httpx.AsyncClient, products_world: World, value: Any
) -> None:
    response = await api.post(
        PRODUCTS,
        json=site_body("S12-SCALAR", attributes={"color": value}),
        headers=_acme(products_world),
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["attributes"]["color"] == value
    got = await api.get(f"{PRODUCTS}/{created['id']}", headers=_acme(products_world))
    assert got.status_code == 200, got.text
    assert got.json()["attributes"]["color"] == value
    assert type(got.json()["attributes"]["color"]) is type(value)


async def test_huge_integer_literal_never_500(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    raw = json.dumps(site_body("S12-BIG")).replace('"navy blue"', "9" * 5000)
    response = await _post_raw(api, products_world, raw)
    assert response.status_code in (201, 400, 422), response.text


async def test_duplicate_json_key_resolves_once(
    api: httpx.AsyncClient, products_world: World
) -> None:
    raw = (
        '{"sku":"S12-DUPKEY","title":"T","category":"kurtas",'
        '"attributes":{"color":"red","color":"blue"}}'
    )
    response = await _post_raw(api, products_world, raw)
    assert response.status_code in (201, 422), response.text
    if response.status_code == 201:
        assert response.json()["attributes"] == {"color": "blue"}


# ---- claims: the role and tenant come from the membership, never widened by the token ----


async def test_forged_role_claim_cannot_create(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    headers = bearer_for(products_world, "reviewer@acme.test", role="owner")
    response = await api.post(PRODUCTS, json=site_body("S12-FORGE"), headers=headers)
    assert response.status_code in (401, 403), response.text
    assert counts(auth_db) == (0, 0)


async def test_foreign_tid_claim_cannot_read_or_create(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await api.post(PRODUCTS, json=site_body("S12-TID"), headers=_acme(products_world))
    assert created.status_code == 201
    # a bharat user whose token names acme's tenant: not a member there
    headers = bearer_for(products_world, "owner@bharat.test", tenant="acme")
    got = await api.get(f"{PRODUCTS}/{created.json()['id']}", headers=headers)
    assert got.status_code in (401, 403, 404), got.text
    listed = await api.get(PRODUCTS, headers=headers)
    assert listed.status_code in (401, 403), listed.text
    post = await api.post(PRODUCTS, json=site_body("S12-TID2"), headers=headers)
    assert post.status_code in (401, 403), post.text
    assert counts(auth_db) == (1, 1)


# ---- cursors: extreme or hand-made keysets are a 422 or an honest page, never a 500 ----


@pytest.mark.parametrize(
    "stamp",
    [
        "0001-01-01T00:00:00+05:30",
        "0001-01-01T00:00:00+00:00",
        "9999-12-31T23:59:59.999999-05:00",
        "9999-12-31T23:59:59.999999+00:00",
        "2026-10-06T00:00:00+23:59",
    ],
)
async def test_extreme_cursor_stamp_never_500(
    api: httpx.AsyncClient, products_world: World, stamp: str
) -> None:
    cursor = _cursor(stamp, str(uuid.uuid4()))
    response = await api.get(PRODUCTS, params={"cursor": cursor}, headers=_acme(products_world))
    assert response.status_code in (200, 422), response.text


async def test_handmade_cursor_never_leaks_foreign_rows(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    insert_products(auth_db, products_world.tenant("bharat"), 5, prefix="S12-BH")
    cursor = _cursor("9999-01-01T00:00:00+00:00", "ffffffff-ffff-ffff-ffff-ffffffffffff")
    response = await api.get(PRODUCTS, params={"cursor": cursor}, headers=_acme(products_world))
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []


async def test_get_foreign_id_under_every_acme_role_is_404(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    (foreign,) = insert_products(auth_db, products_world.tenant("bharat"), 1, prefix="S12-FX")
    for role in ("owner", "operator", "reviewer", "admin"):
        response = await api.get(f"{PRODUCTS}/{foreign}", headers=_acme(products_world, role))
        assert response.status_code == 404, response.text
        assert "S12-FX" not in response.text


async def test_tenant_id_in_query_is_ignored(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    insert_products(auth_db, products_world.tenant("bharat"), 3, prefix="S12-QX")
    response = await api.get(
        PRODUCTS,
        params={"tenant_id": str(products_world.tenant("bharat"))},
        headers=_acme(products_world),
    )
    assert response.status_code in (200, 422), response.text
    if response.status_code == 200:
        assert response.json()["data"] == []
