"""S12 T9 (AC1, AC2): `POST /api/v1/products` through the real app as `app_rw`: success,
validation, missing or invalid tokens, wrong roles, the fault order, and token trust (the row,
never the token, decides tenant membership and role)."""

from __future__ import annotations

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
    counts,
    site_body,
    without_correlation,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _row(conn: Conn, sku: str) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT tenant_id, created_by, status FROM products WHERE sku = %s", [sku]
    ).fetchone()


@pytest.mark.endpoint("products_create", case="success")
async def test_products_create_success(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    response = await api.post(
        PRODUCTS, json=site_body(), headers=bearer_for(products_world, "operator@acme.test")
    )
    assert response.status_code == 201, response.text
    out = response.json()
    assert set(out) == PRODUCT_FIELDS
    assert out["sku"] == "S12-KURTA-1"
    assert out["title"] == "Organic Cotton Kurta"
    assert out["status"] == "draft"
    assert out["category"] == "kurtas"
    assert out["attributes"] == {"color": "navy blue", "fabric_type": "organic cotton"}
    assert out["images"] == []
    assert out["parent_product_id"] is None
    assert out["created_at"] and out["updated_at"]
    row = _row(auth_db, "S12-KURTA-1")
    assert row == (
        products_world.tenant("acme"),
        products_world.user("operator@acme.test"),
        "draft",
    )
    assert str(row[0]) and out["id"]


async def test_products_create_owner_ok(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.post(
        PRODUCTS, json=site_body("S12-OWNER"), headers=bearer_for(products_world, "owner@acme.test")
    )
    assert response.status_code == 201, response.text


_VALIDATION: dict[str, Any] = {
    "extra_tenant_id": site_body("S12-V1", tenant_id="00000000-0000-0000-0000-000000000001"),
    "sku_65": site_body("S" * 65),
    "sku_space": site_body("S12 SPACE"),
    "sku_bracket": site_body("S12]X"),
    "blank_title": site_body("S12-V2", title="   "),
    "nested_attribute": site_body("S12-V3", attributes={"color": {"value": "red"}}),
    "nan": '{"sku": "S12-V4", "title": "T", "category": "kurtas", "attributes": {"color": NaN}}',
    "nul_sku": site_body("S12-\u0000"),
    "nul_title": site_body("S12-V5", title="Kurta\u0000"),
    "nul_attr_value": site_body("S12-V6", attributes={"color": "red\u0000"}),
    "no_category": {"sku": "S12-V7", "title": "T"},
}
_NUL_LOC = {
    "nul_sku": ["body", "sku"],
    "nul_title": ["body", "title"],
    "nul_attr_value": ["body", "attributes", "color"],
    "no_category": ["body", "category"],
}


@pytest.mark.endpoint("products_create", case="validation")
@pytest.mark.parametrize("case", sorted(_VALIDATION))
async def test_products_create_validation(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, case: str
) -> None:
    before = counts(auth_db)
    body = _VALIDATION[case]
    headers = bearer_for(products_world, "operator@acme.test")
    if isinstance(body, str):
        response = await api.post(
            PRODUCTS, content=body, headers={**headers, "Content-Type": "application/json"}
        )
    else:
        response = await api.post(PRODUCTS, json=body, headers=headers)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["type"] == "VALIDATION_FAILED"
    if case in _NUL_LOC:
        assert error["details"] == [{"loc": _NUL_LOC[case], "msg": "Invalid value."}]
    assert "\u0000" not in response.text
    assert counts(auth_db) == before


@pytest.mark.endpoint("products_create", case="missing_auth")
async def test_products_create_missing_auth(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    response = await api.post(PRODUCTS, json=site_body("S12-NOAUTH"))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"
    assert counts(auth_db) == (0, 0)


async def _invalid(api: httpx.AsyncClient, world: World, conn: Conn, variant: str) -> None:
    response = await api.post(
        PRODUCTS, json=site_body("S12-BADTOK"), headers=bad_bearer(world, variant)
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"
    assert counts(conn) == (0, 0)


@pytest.mark.endpoint("products_create", case="invalid_token", variant="expired")
async def test_products_create_invalid_token_expired(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _invalid(api, products_world, auth_db, "expired")


@pytest.mark.endpoint("products_create", case="invalid_token", variant="bad_signature")
async def test_products_create_invalid_token_bad_signature(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _invalid(api, products_world, auth_db, "bad_signature")


@pytest.mark.endpoint("products_create", case="invalid_token", variant="wrong_typ")
async def test_products_create_invalid_token_wrong_typ(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _invalid(api, products_world, auth_db, "wrong_typ")


@pytest.mark.endpoint("products_create", case="wrong_role")
@pytest.mark.parametrize("role", ["reviewer", "admin"])
async def test_products_create_wrong_role(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, role: str
) -> None:
    response = await api.post(
        PRODUCTS,
        json=site_body("S12-ROLE"),
        headers=bearer_for(products_world, f"{role}@acme.test"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert counts(auth_db) == (0, 0)


# ---- fault order (s3 CODE-PLAN:80-83): only malformed JSON is 422 before auth ----


async def test_products_create_schema_invalid_unauthenticated_401(
    api: httpx.AsyncClient, products_world: World
) -> None:
    response = await api.post(PRODUCTS, json=site_body("S12-F1", tenant_id="x"))
    assert response.status_code == 401


async def test_products_create_schema_invalid_wrong_role_403(
    api: httpx.AsyncClient, products_world: World
) -> None:
    response = await api.post(
        PRODUCTS,
        json=site_body("S12-F2", tenant_id="x"),
        headers=bearer_for(products_world, "reviewer@acme.test"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"


async def test_products_create_malformed_json_422_before_auth(
    api: httpx.AsyncClient, products_world: World
) -> None:
    response = await api.post(
        PRODUCTS, content=b'{"sku": ', headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422


# ---- token trust ----


async def test_products_create_tid_without_membership(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    forged = bearer_for(products_world, "operator@acme.test", tenant="bharat")
    response = await api.post(PRODUCTS, json=site_body("S12-TID"), headers=forged)
    assert response.status_code == 401
    missing = await api.post(PRODUCTS, json=site_body("S12-TID"))
    assert without_correlation(response.json()) == without_correlation(missing.json())
    assert counts(auth_db) == (0, 0)


async def test_products_create_role_claim_ignored(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    lying = bearer_for(products_world, "reviewer@acme.test", role="operator")
    response = await api.post(PRODUCTS, json=site_body("S12-LIE"), headers=lying)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert counts(auth_db) == (0, 0)
