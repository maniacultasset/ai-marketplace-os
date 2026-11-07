"""SI1 T12 (AC3-AC5, AC18): `PATCH /api/v1/products/{product_id}` under `If-Match`, as app_rw
through the real app. Run in the Docker batch (`make api-tests-integration`)."""

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
from .products_support import (
    PRODUCTS,
    Conn,
    bad_bearer,
    bearer_for,
    has_phone,
    site_body,
    wire_chart,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PARTY: dict[str, Any] = {
    "legal_name": "Acme Garments Pvt Ltd",
    "address_line1": "12 MG Road",
    "city": "Bengaluru",
    "state_code": "KA",
    "postal_code": "560001",
    "gstin": "29ABCDE1234F1Z5",
    "email": "care@acme.example",
    "phone": "+91 80 1234 5678",
}
CHART: dict[str, Any] = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}


def _op(world: World, email: str = "operator@acme.test") -> dict[str, str]:
    return bearer_for(world, email)


async def _create(api: httpx.AsyncClient, world: World, sku: str, **over: Any) -> dict[str, Any]:
    response = await api.post(PRODUCTS, json=site_body(sku, **over), headers=_op(world))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    body["_etag"] = response.headers["ETag"]
    return body


async def _patch(
    api: httpx.AsyncClient,
    world: World,
    product_id: Any,
    body: object,
    *,
    etag: str | None = None,
    email: str = "operator@acme.test",
) -> httpx.Response:
    if etag is None:
        got = await api.get(f"{PRODUCTS}/{product_id}", headers=_op(world, email))
        etag = got.headers.get("ETag", '"' + "0" * 36 + '"')
    return await api.patch(
        f"{PRODUCTS}/{product_id}", json=body, headers={**_op(world, email), "If-Match": etag}
    )


def _updated_at(conn: Conn, product_id: Any) -> Any:
    row = conn.execute("SELECT updated_at FROM products WHERE id = %s", [product_id]).fetchone()
    assert row is not None
    return row[0]


def _fact_audits(conn: Conn, product_id: Any) -> list[tuple[Any, ...]]:
    return [r for r in audit_rows(conn, "product.fact_updated") if str(r[5]) == str(product_id)]


# ---- the case matrix ----


@pytest.mark.endpoint("products_update", case="success")
async def test_products_update_success(api: httpx.AsyncClient, products_world: World) -> None:
    created = await _create(api, products_world, "SI1-P-OK")
    response = await _patch(
        api,
        products_world,
        created["id"],
        {
            "title": "New Title",
            "style_number": "ST-1",
            "mrp": "1499.00",
            "selling_price": "1299.5",
            "manufacturer": PARTY,
            "size_chart": CHART,
            "attributes": {"fabric_composition": "100% Cotton"},
        },
        etag=created["_etag"],
    )
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["title"] == "New Title"
    assert (out["style_number"], out["mrp"], out["selling_price"]) == (
        "ST-1",
        "1499.00",
        "1299.50",
    )
    assert out["manufacturer"]["legal_name"] == PARTY["legal_name"]
    assert out["size_chart"] == wire_chart(CHART)
    assert out["fiber_composition"] == [{"fibre": "cotton", "percent": 100}]
    assert response.headers["ETag"] != created["_etag"]


@pytest.mark.endpoint("products_update", case="validation")
@pytest.mark.parametrize(
    "body",
    [{"sku": "NEW"}, {"mrp": "0"}, {"mrp": "1e3"}, {"title": None}, {"attributes": None}],
)
async def test_products_update_validation(
    api: httpx.AsyncClient, products_world: World, body: dict[str, Any]
) -> None:
    created = await _create(api, products_world, f"SI1-P-V-{uuid.uuid4().hex[:6]}")
    response = await _patch(api, products_world, created["id"], body, etag=created["_etag"])
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


@pytest.mark.endpoint("products_update", case="missing_auth")
async def test_products_update_missing_auth(api: httpx.AsyncClient, products_world: World) -> None:
    created = await _create(api, products_world, "SI1-P-NOAUTH")
    response = await api.patch(
        f"{PRODUCTS}/{created['id']}", json={"title": "x"}, headers={"If-Match": created["_etag"]}
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.endpoint("products_update", case="invalid_token", variant="expired")
@pytest.mark.endpoint("products_update", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("products_update", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_products_update_invalid_token(
    api: httpx.AsyncClient, products_world: World, variant: str
) -> None:
    created = await _create(api, products_world, f"SI1-P-TOK-{variant}")
    response = await api.patch(
        f"{PRODUCTS}/{created['id']}",
        json={"title": "x"},
        headers={**bad_bearer(products_world, variant), "If-Match": created["_etag"]},
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("products_update", case="wrong_tenant")
async def test_products_update_wrong_tenant(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await _create(api, products_world, "SI1-P-XT")
    before = _updated_at(auth_db, created["id"])
    response = await api.patch(
        f"{PRODUCTS}/{created['id']}",
        json={"title": "stolen"},
        headers={**_op(products_world, "operator@bharat.test"), "If-Match": created["_etag"]},
    )
    assert response.status_code == 404
    missing = await api.patch(
        f"{PRODUCTS}/{uuid.uuid4()}",
        json={"title": "stolen"},
        headers={**_op(products_world, "operator@bharat.test"), "If-Match": created["_etag"]},
    )
    assert missing.status_code == 404
    assert response.json()["error"]["message"] == missing.json()["error"]["message"]
    assert _updated_at(auth_db, created["id"]) == before


@pytest.mark.endpoint("products_update", case="wrong_role")
async def test_products_update_wrong_role(api: httpx.AsyncClient, products_world: World) -> None:
    created = await _create(api, products_world, "SI1-P-ROLE")
    response = await _patch(
        api,
        products_world,
        created["id"],
        {"title": "x"},
        etag=created["_etag"],
        email="reviewer@acme.test",
    )
    assert response.status_code == 403


@pytest.mark.endpoint("products_update", case="not_found")
async def test_products_update_not_found(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.patch(
        f"{PRODUCTS}/{uuid.uuid4()}",
        json={"title": "x"},
        headers={**_op(products_world), "If-Match": '"' + "0" * 36 + '"'},
    )
    assert response.status_code == 404


@pytest.mark.endpoint("products_update", case="conflict")
async def test_products_update_stale_etag_409(
    api: httpx.AsyncClient, products_world: World
) -> None:
    created = await _create(api, products_world, "SI1-P-STALE")
    first = await _patch(api, products_world, created["id"], {"title": "A"}, etag=created["_etag"])
    assert first.status_code == 200
    stale = await _patch(api, products_world, created["id"], {"title": "B"}, etag=created["_etag"])
    assert stale.status_code == 409
    assert stale.json()["error"]["type"] == "STALE_VERSION"


# ---- D1b behaviour ----


async def test_identical_body_stale_tag_is_200(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await _create(api, products_world, "SI1-P-NOOP", mrp="999.00")
    stale = created["_etag"]
    moved = await _patch(api, products_world, created["id"], {"title": "Moved"}, etag=stale)
    assert moved.status_code == 200
    before = _updated_at(auth_db, created["id"])
    audits = len(_fact_audits(auth_db, created["id"]))
    again = await _patch(api, products_world, created["id"], {"title": "Moved"}, etag=stale)
    assert again.status_code == 200, again.text
    assert again.headers["ETag"] == moved.headers["ETag"]
    assert _updated_at(auth_db, created["id"]) == before
    assert len(_fact_audits(auth_db, created["id"])) == audits


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("style_number", "ST-9"),
        ("mrp", "1999.00"),
        ("selling_price", "999.00"),
        ("size_chart", CHART),
        ("manufacturer", PARTY),
    ],
)
async def test_field_set_changed_cleared(
    api: httpx.AsyncClient, products_world: World, field: str, value: Any
) -> None:
    created = await _create(api, products_world, f"SI1-P-F-{field}", mrp="2999.00")
    pid = created["id"]
    set_ = await _patch(api, products_world, pid, {field: value})
    assert set_.status_code == 200, set_.text
    assert set_.json()[field] is not None
    cleared = await _patch(api, products_world, pid, {field: None})
    assert cleared.status_code == 200, cleared.text
    if field == "manufacturer":
        manufacturer = cleared.json()["manufacturer"]
        assert manufacturer is None or manufacturer["inherited"] is True
    else:
        assert cleared.json()[field] is None


async def test_attribute_removed(api: httpx.AsyncClient, products_world: World) -> None:
    created = await _create(api, products_world, "SI1-P-ATTR")
    response = await _patch(api, products_world, created["id"], {"attributes": {"color": None}})
    assert response.status_code == 200, response.text
    assert "color" not in response.json()["attributes"]


async def test_document_attribute_not_cleared(
    api: httpx.AsyncClient, products_world: World
) -> None:
    acme = products_world.tenant("acme")
    pid = f.product(
        products_world.conn,
        acme,
        attributes={
            "color": {
                "value": "red",
                "source": "document",
                "captured_via": "import",
                "evidence_ref": "image:abc",
                "updated_at": "2026-11-06T00:00:00Z",
            }
        },
    )
    response = await _patch(api, products_world, pid, {"attributes": {"color": None}})
    assert response.status_code == 422


async def _family(world: World, **style_columns: Any) -> tuple[uuid.UUID, uuid.UUID]:
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


async def test_style_field_on_variant_refused(
    api: httpx.AsyncClient, products_world: World
) -> None:
    _, child = await _family(products_world)
    response = await _patch(api, products_world, child, {"style_number": "ST-1"})
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "FACT_LEVEL_INVALID"


async def test_size_chart_on_variant_refused(api: httpx.AsyncClient, products_world: World) -> None:
    _, child = await _family(products_world)
    response = await _patch(api, products_world, child, {"size_chart": CHART})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "FACT_LEVEL_INVALID"
    assert error["details"][0]["loc"] == ["body", "size_chart"]


async def test_mrp_on_variant_allowed(api: httpx.AsyncClient, products_world: World) -> None:
    _, child = await _family(products_world)
    response = await _patch(api, products_world, child, {"mrp": "999.00"})
    assert response.status_code == 200, response.text


async def test_variant_price_above_style_mrp_refused(
    api: httpx.AsyncClient, products_world: World
) -> None:
    _, child = await _family(
        products_world,
        mrp="999.00",
        provenance={"title": f.fact(), "category": f.fact(), "mrp": f.fact()},
    )
    response = await _patch(api, products_world, child, {"selling_price": "1000.00"})
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "selling_price"]


async def test_style_mrp_under_child_price_refused(
    api: httpx.AsyncClient, products_world: World
) -> None:
    style, child = await _family(products_world)
    products_world.conn.execute(
        "UPDATE products SET selling_price = 900, provenance = provenance || "
        "jsonb_build_object('selling_price', %s::jsonb) WHERE id = %s",
        [json.dumps(f.fact()), child],
    )
    response = await _patch(api, products_world, style, {"mrp": "899.00"})
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "mrp"]


async def test_style_price_over_child_own_mrp_refused(
    api: httpx.AsyncClient, products_world: World
) -> None:
    # round 1 fix: a style price raise loads the children and checks a child that has its own
    # MRP but inherits the style's price (CP14)
    style, child = await _family(
        products_world,
        mrp="2000.00",
        selling_price="1000.00",
        provenance={
            "title": f.fact(),
            "category": f.fact(),
            "mrp": f.fact(),
            "selling_price": f.fact(),
        },
    )
    products_world.conn.execute(
        "UPDATE products SET mrp = 1200, provenance = provenance || "
        "jsonb_build_object('mrp', %s::jsonb) WHERE id = %s",
        [json.dumps(f.fact()), child],
    )
    response = await _patch(api, products_world, style, {"selling_price": "1500.00"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"][0]["loc"] == ["body", "selling_price"]
    ok = await _patch(api, products_world, style, {"selling_price": "1100.00"})
    assert ok.status_code == 200, ok.text


async def test_category_with_listing_refused(api: httpx.AsyncClient, products_world: World) -> None:
    created = await _create(api, products_world, "SI1-P-CAT")
    f.listing(products_world.conn, products_world.tenant("acme"), uuid.UUID(created["id"]))
    response = await _patch(api, products_world, created["id"], {"category": "t_shirts"})
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "category"]
    plain = await _create(api, products_world, "SI1-P-CAT2")
    ok = await _patch(api, products_world, plain["id"], {"category": "t_shirts"})
    assert ok.status_code == 200, ok.text


async def test_operator_patch_cannot_rewrite_party(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    seeded = await _create(api, products_world, "SI1-P-AC6-A", manufacturer=PARTY)
    tenant = products_world.tenant("acme")
    query = "SELECT * FROM business_parties WHERE legal_name = %s AND tenant_id = %s"
    before = auth_db.execute(query, [PARTY["legal_name"], tenant]).fetchall()
    target = await _create(api, products_world, "SI1-P-AC6-B")
    moved = {**PARTY, "city": "Mysuru", "state_code": "TN", "gstin": "33ABCDE1234F1Z5"}
    response = await _patch(api, products_world, target["id"], {"manufacturer": moved})
    assert response.status_code == 200, response.text
    assert auth_db.execute(query, [PARTY["legal_name"], tenant]).fetchall() == before
    assert response.json()["manufacturer"]["id"] == seeded["manufacturer"]["id"]


async def test_audit_changed_keys_only(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await _create(api, products_world, "SI1-P-AUD", mrp="999.00")
    response = await _patch(
        api, products_world, created["id"], {"mrp": "1099.00", "manufacturer": PARTY}
    )
    assert response.status_code == 200, response.text
    rows = _fact_audits(auth_db, created["id"])
    assert len(rows) == 1
    after = rows[0][7]
    assert set(after) <= {"attributes", "provenance"}
    assert set(after["provenance"]) == {"mrp", "manufacturer"}
    text = json.dumps(after)
    assert "@" not in text and not has_phone(text) and "phone" not in text
    assert "postal_code" not in text and PARTY["legal_name"] not in text


async def test_concurrent_patches_one_wins(api: httpx.AsyncClient, products_world: World) -> None:
    created = await _create(api, products_world, "SI1-P-RACE")
    tag = created["_etag"]
    results = await asyncio.gather(
        _patch(api, products_world, created["id"], {"title": "One"}, etag=tag),
        _patch(api, products_world, created["id"], {"title": "Two"}, etag=tag),
    )
    assert sorted(r.status_code for r in results) == [200, 409]


# ---- SI3 T15: confirm, reject and the photo facts on reads ----

PHOTO_IMAGE = "6f1d2c3b-8a9e-4b70-9c55-2e4f6a8b0d11"


def _photo(value: str = "v-neck") -> dict[str, Any]:
    return {
        "value": value,
        "normalized": value,
        "source": "ai",
        "captured_via": "agent",
        "agent_run_id": str(uuid.uuid4()),
        "confidence": 0.95,
        "evidence_ref": f"image:{PHOTO_IMAGE}",
        "updated_at": "2026-11-07T00:00:00.000000Z",
    }


def _put_photo(conn: Conn, product_id: Any, key: str = "neckline") -> None:
    from psycopg.types.json import Jsonb

    conn.execute(
        "UPDATE products SET attributes = attributes || jsonb_build_object(%s::text, %s::jsonb) "
        "WHERE id = %s",
        [key, Jsonb(_photo()), product_id],
    )


def _stored(conn: Conn, product_id: Any) -> dict[str, Any]:
    row = conn.execute("SELECT attributes FROM products WHERE id = %s", [product_id]).fetchone()
    assert row is not None
    return dict(row[0])


async def test_si3_confirm_a_photo_value(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await _create(api, products_world, "SI3-CONFIRM")
    _put_photo(auth_db, created["id"])
    got = await api.get(f"{PRODUCTS}/{created['id']}", headers=_op(products_world))
    (fact,) = got.json()["photo_facts"]
    assert fact == {
        "key": "neckline",
        "value": "v-neck",
        "confidence": 0.95,
        "image_id": PHOTO_IMAGE,
        "confirmed": False,
        "confirmed_at": None,
    }
    response = await _patch(
        api,
        products_world,
        created["id"],
        {"confirm_attributes": ["neckline"]},
        etag=got.headers["ETag"],
    )
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] != got.headers["ETag"]
    (fact,) = response.json()["photo_facts"]
    assert fact["confirmed"] is True and fact["confirmed_at"] is not None
    stored = _stored(auth_db, created["id"])["neckline"]
    assert stored["confirmed_by"] == str(products_world.user("operator@acme.test"))
    assert stored["value"] == "v-neck"
    after = auth_db.execute(
        "SELECT after FROM audit_log WHERE entity_id = %s AND action = 'product.fact_updated' "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1",
        [created["id"]],
    ).fetchone()
    assert after is not None and after[0]["attributes"]["neckline"]["confirmed_at"] is not None


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"confirm_attributes": ["color"]}, "VALIDATION_FAILED"),
        ({"confirm_attributes": ["sparkle"]}, "UNKNOWN_FACT_KEY"),
        (
            {"confirm_attributes": ["neckline"], "attributes": {"neckline": "Round"}},
            "VALIDATION_FAILED",
        ),
    ],
)
async def test_si3_confirm_refusals(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, body: dict[str, Any], code: str
) -> None:
    created = await _create(api, products_world, f"SI3-REF-{uuid.uuid4().hex[:6]}")
    _put_photo(auth_db, created["id"])
    response = await _patch(api, products_world, created["id"], body)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["type"] == code
    assert error["details"][0]["loc"] == ["body", "confirm_attributes", 0]


async def test_si3_reject_removes_the_photo_value(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await _create(api, products_world, "SI3-REJECT")
    _put_photo(auth_db, created["id"])
    response = await _patch(api, products_world, created["id"], {"attributes": {"neckline": None}})
    assert response.status_code == 200, response.text
    assert response.json()["photo_facts"] == []
    assert "neckline" not in _stored(auth_db, created["id"])


async def test_si3_a_seller_write_cancels_the_open_question(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    from app.db.session import unit_of_work
    from app.repositories.human_tasks import HumanTaskRepository

    created = await _create(api, products_world, "SI3-CANCEL")
    tenant = products_world.tenant("acme")
    product_id = uuid.UUID(created["id"])
    async with unit_of_work(tenant) as session:
        assert await HumanTaskRepository(session, tenant).open_photo_fact_task(
            product_id=product_id,
            kind="missing_fact",
            target_path="facts.attributes.neckline",
            question="What is the neckline of this product?",
            reason="The photos suggest v-neck, but not clearly enough to use it.",
            expected_answer={"kind": "enum", "allowed_values": ["v-neck"]},
            suggestions=[],
            evidence=[],
        )
    response = await _patch(
        api, products_world, created["id"], {"attributes": {"neckline": "V-Neck"}}
    )
    assert response.status_code == 200, response.text
    row = auth_db.execute(
        "SELECT status, cancelled_reason FROM human_tasks WHERE product_id = %s", [product_id]
    ).fetchone()
    assert row == ("cancelled", "superseded")


async def test_si3_confirm_with_a_stale_tag_is_409(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await _create(api, products_world, "SI3-STALE")
    _put_photo(auth_db, created["id"])
    response = await _patch(
        api,
        products_world,
        created["id"],
        {"confirm_attributes": ["neckline"]},
        etag=created["_etag"],
    )
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "STALE_VERSION"


async def test_si3_confirm_role_and_tenant(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    created = await _create(api, products_world, "SI3-ROLE")
    _put_photo(auth_db, created["id"])
    response = await _patch(
        api,
        products_world,
        created["id"],
        {"confirm_attributes": ["neckline"]},
        email="reviewer@acme.test",
    )
    assert response.status_code == 403
    response = await _patch(
        api,
        products_world,
        created["id"],
        {"confirm_attributes": ["neckline"]},
        email="operator@bharat.test",
    )
    assert response.status_code == 404
    assert "confirmed_at" not in _stored(auth_db, created["id"])["neckline"]
