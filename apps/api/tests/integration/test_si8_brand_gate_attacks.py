"""SI8 tester round 1 (AC10; CP-D8, CP-D9): attacks on the brand and audience API fields through
the real app as app_rw: `null` on a variant, padded and cased brands, another tenant's brand
profile names, Unicode names, a profile rename after an override, the style's audience on a
variant, and role escalation."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bearer_for, site_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _op(world: World, email: str = "operator@acme.test") -> dict[str, str]:
    return bearer_for(world, email)


def _profile(conn: Conn, tenant: Any, names: list[str]) -> None:
    conn.execute(
        "INSERT INTO brand_profiles (tenant_id, brand_names, provenance) VALUES (%s, %s, '{}') "
        "ON CONFLICT (tenant_id) DO UPDATE SET brand_names = EXCLUDED.brand_names",
        [tenant, names],
    )


async def _create(api: httpx.AsyncClient, world: World, sku: str, **over: Any) -> httpx.Response:
    return await api.post(PRODUCTS, json=site_body(sku, **over), headers=_op(world))


async def _patch(
    api: httpx.AsyncClient, world: World, product_id: Any, body: object, **kw: Any
) -> httpx.Response:
    email = kw.get("email", "operator@acme.test")
    got = await api.get(f"{PRODUCTS}/{product_id}", headers=_op(world))
    return await api.patch(
        f"{PRODUCTS}/{product_id}",
        json=body,
        headers={**_op(world, email), "If-Match": got.headers["ETag"]},
    )


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


def _stored(conn: Conn, product_id: Any) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT brand_name, gender FROM products WHERE id = %s", [product_id]
    ).fetchone()
    assert row is not None
    return tuple(row)


@pytest.mark.parametrize("body", [{"brand_name": None}, {"gender": None}])
async def test_null_brand_or_gender_on_a_variant_is_fact_level_invalid(
    api: httpx.AsyncClient, products_world: World, body: dict[str, Any]
) -> None:
    _, child = _family(products_world, brand_name="Acme", gender="women")
    response = await _patch(api, products_world, child, body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "FACT_LEVEL_INVALID"


@pytest.mark.parametrize("given", ["  acme  ", "ACME", "aCmE"])
async def test_padded_or_cased_brand_is_stored_in_the_names_spelling(
    api: httpx.AsyncClient, products_world: World, given: str
) -> None:
    response = await _create(api, products_world, "SI8-T-CASE", brand_name=given)
    assert response.status_code == 201, response.text
    assert response.json()["brand_name"] == "Acme"
    assert _stored(products_world.conn, response.json()["id"])[0] == "Acme"


async def test_another_tenants_profile_name_is_refused_on_create_and_patch(
    api: httpx.AsyncClient, products_world: World
) -> None:
    _profile(products_world.conn, products_world.tenant("bharat"), ["Desi Loom"])
    created = await _create(api, products_world, "SI8-T-X1", brand_name="Desi Loom")
    assert created.status_code == 422, created.text
    assert created.json()["error"]["type"] == "VALUE_NOT_ALLOWED"
    ok = await _create(api, products_world, "SI8-T-X2")
    assert ok.status_code == 201
    assert ok.json()["brand_name"] == "Acme"  # never bharat's profile name
    patched = await _patch(api, products_world, ok.json()["id"], {"brand_name": "desi loom"})
    assert patched.status_code == 422
    assert _stored(products_world.conn, ok.json()["id"]) == (None, None)


async def test_unicode_profile_name_matches_casefold(
    api: httpx.AsyncClient, products_world: World
) -> None:
    _profile(products_world.conn, products_world.tenant("acme"), ["", "  ", "Straße कुर्ता"])
    created = await _create(api, products_world, "SI8-T-U1")
    assert created.status_code == 201
    assert (created.json()["brand_name"], created.json()["brand_name_inherited"]) == (
        "Straße कुर्ता",
        True,
    )
    patched = await _patch(
        api, products_world, created.json()["id"], {"brand_name": "STRASSE कुर्ता"}
    )
    assert patched.status_code == 200, patched.text
    assert (patched.json()["brand_name"], patched.json()["brand_name_inherited"]) == (
        "Straße कुर्ता",
        False,
    )


async def test_profile_rename_after_an_override_keeps_the_stored_brand(
    api: httpx.AsyncClient, products_world: World
) -> None:
    _profile(products_world.conn, products_world.tenant("acme"), ["Kuinn"])
    created = await _create(api, products_world, "SI8-T-R1", brand_name="kuinn")
    assert created.status_code == 201
    _profile(products_world.conn, products_world.tenant("acme"), ["Kuinn Studio"])
    got = (await api.get(f"{PRODUCTS}/{created.json()['id']}", headers=_op(products_world))).json()
    assert (got["brand_name"], got["brand_name_inherited"]) == ("Kuinn", False)
    # an unrelated PATCH still succeeds: the gate checks new brand writes only (CP-D9)
    patched = await _patch(api, products_world, created.json()["id"], {"gender": "men"})
    assert patched.status_code == 200, patched.text
    assert _stored(products_world.conn, created.json()["id"]) == ("Kuinn", "men")


async def test_style_patch_reaches_the_variant_read(
    api: httpx.AsyncClient, products_world: World
) -> None:
    style, child = _family(products_world)
    response = await _patch(api, products_world, style, {"gender": "girls", "brand_name": "Acme"})
    assert response.status_code == 200, response.text
    got = (await api.get(f"{PRODUCTS}/{child}", headers=_op(products_world))).json()
    assert (got["gender"], got["brand_name"], got["brand_name_inherited"]) == (
        "girls",
        "Acme",
        False,
    )
    assert _stored(products_world.conn, child) == (None, None)


async def test_reviewer_cannot_set_brand_or_gender(
    api: httpx.AsyncClient, products_world: World
) -> None:
    email = "reviewer@acme.test"
    created = await _create(api, products_world, "SI8-T-ROLE")
    assert created.status_code == 201
    pid = created.json()["id"]
    response = await _patch(api, products_world, pid, {"gender": "baby"}, email=email)
    assert response.status_code == 403
    posted = await api.post(
        PRODUCTS,
        json=site_body("SI8-T-ROLE2", brand_name="Acme", gender="men"),
        headers=_op(products_world, email),
    )
    assert posted.status_code == 403
    assert _stored(products_world.conn, pid) == (None, None)


@pytest.mark.parametrize("gender", ["Women", "kids", "female", " women", "women\u0000"])
async def test_gender_is_exact_lowercase_literal(
    api: httpx.AsyncClient, products_world: World, gender: str
) -> None:
    response = await _create(api, products_world, "SI8-T-G", gender=gender)
    assert response.status_code == 422, response.text
