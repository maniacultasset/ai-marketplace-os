"""S12 T10 (AC2, AC4, AC5): `POST /api/v1/products` rules: images refused until S17b (CP2), SKU
conflicts (duplicate, concurrent, soft-deleted), seller facts under canonical keys, and the one
`product.created` audit row committed with the product."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from .auth_support import World, audit_rows
from .conftest import LOCAL_HOSTS
from .products_support import (
    PRODUCTS,
    Conn,
    bearer_for,
    counts,
    site_body,
    soft_delete,
    without_correlation,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _op(world: World, email: str = "operator@acme.test") -> dict[str, str]:
    return bearer_for(world, email)


def _scalar(conn: Conn, query: str, params: list[Any]) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


# ---- wrong_tenant and not_found: images (CP2 placeholder until S17b's tenant lookup) ----


@pytest.mark.endpoint("products_create", case="wrong_tenant")
async def test_products_create_wrong_tenant_image_key(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    foreign = await api.post(
        PRODUCTS,
        json=site_body("S12-IMG-1", images=[f"{products_world.tenant('bharat')}/products/a.jpg"]),
        headers=_op(products_world),
    )
    missing = await api.post(
        PRODUCTS,
        json=site_body("S12-IMG-1", images=["no/such/key.jpg"]),
        headers=_op(products_world),
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["details"] is None
    assert without_correlation(foreign.json()) == without_correlation(missing.json())
    assert counts(auth_db) == (0, 0)


@pytest.mark.endpoint("products_create", case="wrong_tenant")
async def test_products_create_wrong_tenant_same_sku_ok(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    bharat = await api.post(
        PRODUCTS, json=site_body("S12-X"), headers=_op(products_world, "operator@bharat.test")
    )
    acme = await api.post(PRODUCTS, json=site_body("S12-X"), headers=_op(products_world))
    assert (bharat.status_code, acme.status_code) == (201, 201)
    assert bharat.json()["id"] != acme.json()["id"]
    tenants = auth_db.execute("SELECT tenant_id FROM products WHERE sku = 'S12-X'").fetchall()
    assert {r[0] for r in tenants} == {
        products_world.tenant("acme"),
        products_world.tenant("bharat"),
    }


@pytest.mark.endpoint("products_create", case="not_found")
async def test_products_create_not_found_image(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    response = await api.post(
        PRODUCTS,
        json=site_body("S12-IMG-2", images=["acme/products/a.jpg"]),
        headers=_op(products_world),
    )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["type"] == "NOT_FOUND"
    assert error["message"] == "images[0] not found."
    assert "acme/products" not in response.text
    assert counts(auth_db) == (0, 0)


# ---- conflict ----


@pytest.mark.endpoint("products_create", case="conflict")
async def test_products_create_conflict_duplicate(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    first = await api.post(PRODUCTS, json=site_body("S12-DUP"), headers=_op(products_world))
    again = await api.post(
        PRODUCTS, json=site_body("S12-DUP", title="Another"), headers=_op(products_world)
    )
    assert (first.status_code, again.status_code) == (201, 409)
    assert again.json()["error"]["type"] == "SKU_CONFLICT"
    assert counts(auth_db) == (1, 1)


async def test_products_create_conflict_concurrent(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    responses = await asyncio.gather(
        *(
            api.post(PRODUCTS, json=site_body("S12-RACE"), headers=_op(products_world))
            for _ in range(2)
        )
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert counts(auth_db) == (1, 1)


async def test_products_create_conflict_soft_deleted_sku(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    first = await api.post(PRODUCTS, json=site_body("S12-SOFT"), headers=_op(products_world))
    soft_delete(auth_db, first.json()["id"])
    again = await api.post(PRODUCTS, json=site_body("S12-SOFT"), headers=_op(products_world))
    assert again.status_code == 409
    assert again.json()["error"]["type"] == "SKU_CONFLICT"


# ---- facts (AC4) ----


async def test_products_create_facts_site_body(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    response = await api.post(PRODUCTS, json=site_body("S12-FACT"), headers=_op(products_world))
    assert response.status_code == 201, response.text
    assert response.json()["attributes"] == {"color": "navy blue", "fabric_type": "organic cotton"}
    attributes, provenance = auth_db.execute(
        "SELECT attributes, provenance FROM products WHERE sku = 'S12-FACT'"
    ).fetchone() or (None, None)
    assert set(attributes) == {"color", "fabric_type"}
    for key in ("color", "fabric_type"):
        fact = attributes[key]
        assert (fact["source"], fact["captured_via"]) == ("seller", "api")
        assert _scalar(auth_db, "SELECT fact_map_valid(%s::jsonb, true)", [json.dumps({key: fact})])
    assert set(provenance) == {"title", "category"}
    assert all("value" not in f for f in provenance.values())


async def test_products_create_facts_unknown_key(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    response = await api.post(
        PRODUCTS,
        json=site_body("S12-UNK", attributes={"shade": "dark"}),
        headers=_op(products_world),
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "UNKNOWN_FACT_KEY"
    assert error["details"] == [{"loc": ["body", "attributes", "shade"], "msg": "Unknown key."}]
    assert "dark" not in response.text
    assert counts(auth_db) == (0, 0)


async def test_products_create_facts_column_key(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    # `title` is column-backed in the dictionary (DATABASE.md:1168); the seed has no such row yet,
    # so this test adds it (dictionary rows are never deleted; it stays column-backed)
    auth_db.execute(
        "INSERT INTO attribute_definitions (key, section, storage, owner_levels, value_type, "
        "input_aliases, is_critical, allowed_sources, confirm_agent_captures, enabled) VALUES "
        "('title', 'identity', 'column', ARRAY['style', 'standalone'], 'text', ARRAY[]::text[], "
        "false, ARRAY['seller'], false, true) ON CONFLICT (key) DO NOTHING"
    )
    response = await api.post(
        PRODUCTS, json=site_body("S12-COL", attributes={"title": "x"}), headers=_op(products_world)
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "VALIDATION_FAILED"
    assert error["details"][0]["loc"] == ["body", "attributes", "title"]
    assert counts(auth_db) == (0, 0)


async def test_products_create_facts_duplicate_canonical(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    body = site_body("S12-DUPK", attributes={"material": "silk", "fabric_type": "cotton"})
    response = await api.post(PRODUCTS, json=body, headers=_op(products_world))
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "attributes", "fabric_type"]
    assert counts(auth_db) == (0, 0)


async def test_products_create_facts_category_given(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    response = await api.post(
        PRODUCTS,
        json=site_body("S12-CAT", category="ethnic_wear_kurta_sets"),
        headers=_op(products_world),
    )
    assert response.status_code == 201
    assert response.json()["category"] == "ethnic_wear_kurta_sets"
    category, provenance = auth_db.execute(
        "SELECT category, provenance FROM products WHERE sku = 'S12-CAT'"
    ).fetchone() or (None, None)
    assert category == "ethnic_wear_kurta_sets"
    assert set(provenance["category"]) == {"source", "captured_via", "updated_at"}


# ---- audit (AC5) ----


async def test_products_create_audit_row(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    response = await api.post(
        PRODUCTS,
        json=site_body("S12-AUDIT"),
        headers={**_op(products_world), "X-Request-ID": "req-s12-audit-0001"},
    )
    assert response.status_code == 201
    rows = audit_rows(auth_db, "product.created")
    assert len(rows) == 1
    tenant, actor_type, actor, email, entity_type, entity_id, before, after, request_id, _ = rows[0]
    assert tenant == products_world.tenant("acme")
    assert (actor_type, actor, email) == ("user", products_world.user("operator@acme.test"), None)
    assert (entity_type, str(entity_id), before) == ("product", response.json()["id"], None)
    assert request_id == "req-s12-audit-0001"
    assert "@" not in json.dumps(after)
    assert set(after) == {"attributes"}
    assert set(after["attributes"]) == {"color", "fabric_type"}
    valid = _scalar(
        auth_db,
        "SELECT fact_map_valid(after -> 'attributes', true) FROM audit_log WHERE action = %s",
        ["product.created"],
    )
    assert valid is True


@pytest.mark.parametrize("status", [422, 403, 409])
async def test_products_create_audit_none_on_refusal(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, status: int
) -> None:
    if status == 409:
        assert (
            await api.post(PRODUCTS, json=site_body("S12-R"), headers=_op(products_world))
        ).status_code == 201
        response = await api.post(PRODUCTS, json=site_body("S12-R"), headers=_op(products_world))
        expected = 1
    elif status == 403:
        response = await api.post(
            PRODUCTS, json=site_body("S12-R"), headers=_op(products_world, "reviewer@acme.test")
        )
        expected = 0
    else:
        response = await api.post(
            PRODUCTS,
            json=site_body("S12-R", attributes={"shade": "x"}),
            headers=_op(products_world),
        )
        expected = 0
    assert response.status_code == status
    assert len(audit_rows(auth_db, "product.created")) == expected


async def test_products_create_audit_injected_failure(
    products_world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.main import create_app
    from app.repositories.identity import AuditRepository

    async def broken(self: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit down CANARY-s12")

    monkeypatch.setattr(AuditRepository, "write", broken)
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            PRODUCTS, json=site_body("S12-BOOM"), headers=_op(products_world)
        )
    assert response.status_code == 500
    assert response.json()["error"]["type"] == "INTERNAL_ERROR"
    assert "CANARY" not in response.text
    assert counts(auth_db) == (0, 0)
