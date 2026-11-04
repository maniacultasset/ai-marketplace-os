"""S20 T19 (AC15; D6): `GET /api/v1/listings/{listing_id}/preview` and its case matrix. The
preview goes through the pure registry previewer: no socket, no marketplace account."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .family_support import family
from .products_support import Conn, bad_bearer, bearer_for
from .seam_support import CATEGORY

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
LISTINGS = "/api/v1/listings"
ROLES = ("owner", "operator", "reviewer", "admin")


def _kurta_v2(conn: Conn) -> uuid.UUID:
    """The seeded Amazon kurta mapping (version 2), as `seed_amazon_mappings` writes it."""
    from app.db.seed_templates import seed_templates

    (seed,) = [s for s in seed_templates() if s.template.category == CATEGORY]
    attributes = seed.template.model_dump(
        mode="json", exclude={"marketplace", "category", "external_category_id"}
    )
    return f.insert(  # type: ignore[no-any-return]
        conn,
        "category_templates",
        marketplace="amazon",
        category=CATEGORY,
        version=seed.version,
        is_current=True,
        product_type=seed.product_type,
        source="manual",
        attributes=attributes,
    )


def _body(image_id: uuid.UUID, **attributes: Any) -> dict[str, Any]:
    return {
        "content": {
            "title": "Navy cotton kurta set",
            "bullets": ["Soft cotton that breathes", "Straight fit"],
            "description": "A straight-fit cotton kurta set with a round neck.",
        },
        "keywords": {"search_terms": ["kurta set"], "hidden_terms": ["cotton kurta"]},
        "attributes": {"fabric_type": "cotton", "color": "navy blue", **attributes},
        "images": [{"image_id": str(image_id), "slot": "main", "position": 0}],
    }


def _listing(
    conn: Conn,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    content: dict[str, Any] | None,
    template_id: uuid.UUID | None,
    marketplace: str = "amazon",
    status: str = "needs_review",
) -> uuid.UUID:
    listing_id = f.listing(
        conn, tenant_id, product_id, category=CATEGORY, marketplace=marketplace, status=status
    )
    if content is not None:
        version_id = f.version(
            conn,
            tenant_id,
            listing_id,
            version=1,
            content_json=content,
            category_template_id=template_id,
        )
        conn.execute(
            "UPDATE listings SET current_version_id = %s WHERE id = %s", [version_id, listing_id]
        )
    return listing_id


def _standalone(world: World, conn: Conn, **over: Any) -> tuple[uuid.UUID, uuid.UUID]:
    acme = world.tenant("acme")
    image_id = uuid.uuid4()
    product_id = f.product(conn, acme, category=CATEGORY, sku="KURTA-PV-1")
    fields: dict[str, Any] = {"content": _body(image_id), "template_id": _kurta_v2(conn)}
    fields.update(over)
    return _listing(conn, acme, product_id, **fields), image_id


async def _get(
    api: httpx.AsyncClient, world: World, listing_id: object, email: str = "admin@acme.test"
) -> httpx.Response:
    return await api.get(f"{LISTINGS}/{listing_id}/preview", headers=bearer_for(world, email))


@pytest.mark.endpoint("listings_preview", case="success")
async def test_listings_preview_success_standalone(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    listing_id, image_id = _standalone(products_world, auth_db)
    response = await _get(api, products_world, listing_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["listing_id"], body["version"], body["marketplace"]) == (
        str(listing_id),
        1,
        "amazon",
    )
    assert body["product_type"] == "KURTA"
    (item,) = body["items"]
    assert (item["seller_sku"], item["item_role"]) == ("KURTA-PV-1", "standalone")
    attributes = item["payload"]["attributes"]
    assert attributes["main_product_image_locator"][0]["media_location"] == f"image:{image_id}"
    assert body["report"]["errors"] == [], body["report"]


@pytest.mark.endpoint("listings_preview", case="success")
async def test_listings_preview_success_family(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    style, kids = family(auth_db, acme, "KFAM-PV", category=CATEGORY)
    content = _body(uuid.uuid4())
    del content["attributes"]["color"]  # a variant axis: each child carries its own colour
    listing_id = _listing(auth_db, acme, style, content=content, template_id=_kurta_v2(auth_db))
    response = await _get(api, products_world, listing_id, "reviewer@acme.test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["product_type"] == "KURTA"
    assert [(i["seller_sku"], i["item_role"]) for i in body["items"]] == [
        ("KFAM-PV", "parent"),
        *((sku, "child") for sku in sorted(kids)),
    ]
    assert body["report"]["errors"] == [], body["report"]


@pytest.mark.endpoint("listings_preview", case="success")
async def test_listings_preview_report_lists_mapping_errors(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    """A blank required attribute and an unmapped template are reported, still 200."""
    listing_id, _ = _standalone(products_world, auth_db, content=_body(uuid.uuid4(), color="  "))
    body = (await _get(api, products_world, listing_id)).json()
    assert {e["field"] for e in body["report"]["errors"]} >= {"attributes.color"}
    assert body["report"]["status"] == "failed"
    acme = products_world.tenant("acme")
    plain = f.product(auth_db, acme, category=CATEGORY)
    unmapped = _listing(auth_db, acme, plain, content=_body(uuid.uuid4()), template_id=None)
    response = await _get(api, products_world, unmapped)
    assert response.status_code == 200
    body = response.json()
    assert body["product_type"] is None
    assert [e["error_code"] for e in body["report"]["errors"]] == ["CATEGORY_NOT_MAPPED"]


@pytest.mark.endpoint("listings_preview", case="validation")
@pytest.mark.parametrize("bad", ["not-a-uuid", "1", "00000000-0000-0000-0000-00000000000g"])
async def test_listings_preview_validation_422(
    api: httpx.AsyncClient, products_world: World, bad: str
) -> None:
    response = await _get(api, products_world, bad)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


@pytest.mark.endpoint("listings_preview", case="missing_auth")
async def test_listings_preview_missing_auth_401(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    listing_id, _ = _standalone(products_world, auth_db)
    response = await api.get(f"{LISTINGS}/{listing_id}/preview")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "KURTA-PV-1" not in response.text


async def _invalid(api: httpx.AsyncClient, world: World, conn: Conn, variant: str) -> None:
    listing_id, _ = _standalone(world, conn)
    response = await api.get(f"{LISTINGS}/{listing_id}/preview", headers=bad_bearer(world, variant))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("listings_preview", case="invalid_token", variant="expired")
async def test_listings_preview_invalid_token_401_expired(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _invalid(api, products_world, auth_db, "expired")


@pytest.mark.endpoint("listings_preview", case="invalid_token", variant="bad_signature")
async def test_listings_preview_invalid_token_401_bad_signature(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _invalid(api, products_world, auth_db, "bad_signature")


@pytest.mark.endpoint("listings_preview", case="invalid_token", variant="wrong_typ")
async def test_listings_preview_invalid_token_401_wrong_typ(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _invalid(api, products_world, auth_db, "wrong_typ")


@pytest.mark.parametrize("role", ROLES)
async def test_listings_preview_na_wrong_role_all_roles_200(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, role: str
) -> None:
    """n/a: every role reads (D6), so the op has no wrong_role case."""
    listing_id, _ = _standalone(products_world, auth_db)
    response = await _get(api, products_world, listing_id, f"{role}@acme.test")
    assert response.status_code == 200, response.text


@pytest.mark.endpoint("listings_preview", case="wrong_tenant")
async def test_listings_preview_wrong_tenant_404(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    listing_id, _ = _standalone(products_world, auth_db)
    headers = {
        **bearer_for(products_world, "admin@bharat.test"),
        "X-Request-ID": "req-s20-xtenant-0001",
    }
    foreign = await api.get(f"{LISTINGS}/{listing_id}/preview", headers=headers)
    missing = await api.get(f"{LISTINGS}/{uuid.uuid4()}/preview", headers=headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.content == missing.content
    assert "KURTA-PV-1" not in foreign.text and str(listing_id) not in foreign.text


@pytest.mark.endpoint("listings_preview", case="not_found")
async def test_listings_preview_not_found_404(
    api: httpx.AsyncClient, products_world: World
) -> None:
    response = await _get(api, products_world, uuid.uuid4())
    assert response.status_code == 404
    error = response.json()["error"]
    assert (error["type"], error["message"]) == ("NOT_FOUND", "listing_id not found.")


@pytest.mark.endpoint("listings_preview", case="conflict")
@pytest.mark.parametrize("kind", ["no_version", "generating"])
async def test_listings_preview_conflict_409(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, kind: str
) -> None:
    if kind == "no_version":
        listing_id, _ = _standalone(products_world, auth_db, content=None, status="created")
    else:
        listing_id, _ = _standalone(products_world, auth_db, status="generating")
    response = await _get(api, products_world, listing_id)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "LISTING_BUSY"


def _flipkart_kurta_v2(conn: Conn) -> uuid.UUID:
    """The seeded Flipkart kurta mapping (version 2), as `seed_flipkart_mappings` writes it."""
    from app.db.seed_templates import flipkart_seed_templates

    (seed,) = [s for s in flipkart_seed_templates() if s.template.category == CATEGORY]
    attributes = seed.template.model_dump(
        mode="json", exclude={"marketplace", "category", "external_category_id"}
    )
    return f.insert(  # type: ignore[no-any-return]
        conn,
        "category_templates",
        marketplace="flipkart",
        category=CATEGORY,
        version=seed.version,
        is_current=True,
        product_type=seed.product_type,
        source="manual",
        attributes=attributes,
    )


def _flipkart_family(world: World, conn: Conn, sku: str = "KFAM-FK") -> tuple[uuid.UUID, Any]:
    acme = world.tenant("acme")
    style, kids = family(conn, acme, sku, category=CATEGORY)
    content = _body(uuid.uuid4())
    del content["attributes"]["color"]
    listing_id = _listing(
        conn, acme, style, content=content, template_id=_flipkart_kurta_v2(conn),
        marketplace="flipkart",
    )  # fmt: skip
    return listing_id, kids


@pytest.mark.endpoint("listings_preview", case="success")
async def test_listings_preview_flipkart_family(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    """S22 T16 (AC15): a Flipkart family previews its 4 children, no parent row, each with its
    style code and colour group; `product_type` is the Flipkart vertical."""
    listing_id, kids = _flipkart_family(products_world, auth_db)
    response = await _get(api, products_world, listing_id, "operator@acme.test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["marketplace"] == "flipkart" and body["product_type"] == "kurta"
    assert [(i["seller_sku"], i["item_role"]) for i in body["items"]] == [
        (sku, "child") for sku in sorted(kids)
    ]
    for item in body["items"]:
        attributes = item["payload"]["attributes"]
        assert attributes["style_code"] == [{"value": "KFAM-FK"}]
        assert attributes["colour_group"][0]["value"] in ("navy blue", "maroon")
    assert body["report"]["errors"] == [], body["report"]


@pytest.mark.endpoint("listings_preview", case="success")
async def test_listings_preview_flipkart_standalone(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    listing_id, image_id = _standalone(
        products_world, auth_db, marketplace="flipkart", template_id=_flipkart_kurta_v2(auth_db)
    )
    response = await _get(api, products_world, listing_id)
    assert response.status_code == 200, response.text
    body = response.json()
    (item,) = body["items"]
    assert (item["seller_sku"], item["item_role"]) == ("KURTA-PV-1", "standalone")
    attributes = item["payload"]["attributes"]
    assert attributes["Colour"] == [{"value": "Navy Blue"}]
    assert attributes["images"] == [{"value": f"image:{image_id}"}]
    assert body["report"]["errors"] == [], body["report"]


async def test_listings_preview_flipkart_unmapped_category(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    listing_id, _ = _standalone(products_world, auth_db, marketplace="flipkart", template_id=None)
    response = await _get(api, products_world, listing_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["product_type"] is None
    assert [e["error_code"] for e in body["report"]["errors"]] == ["CATEGORY_NOT_MAPPED"]


async def test_listings_preview_flipkart_flag_off_200(
    api: httpx.AsyncClient,
    products_world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5: preview stays ungated; it sends nothing whatever the flag says."""
    from app.core.config import get_settings

    monkeypatch.setenv("FLIPKART_ENABLED", "false")
    get_settings.cache_clear()
    listing_id, _ = _flipkart_family(products_world, auth_db)
    response = await _get(api, products_world, listing_id)
    assert response.status_code == 200, response.text
    get_settings.cache_clear()


async def test_listings_preview_flipkart_no_socket_no_account(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    """No Flipkart account exists and only loopback is allowed: the preview sends nothing and
    names no URL or token."""
    listing_id, _ = _flipkart_family(products_world, auth_db)
    assert auth_db.execute("SELECT count(*) FROM marketplace_accounts").fetchone() == (0,)
    response = await _get(api, products_world, listing_id)
    assert response.status_code == 200, response.text
    assert "http://" not in response.text and "https://" not in response.text


@pytest.mark.endpoint("listings_preview", case="wrong_tenant")
async def test_listings_preview_flipkart_wrong_tenant_404(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    listing_id, _ = _flipkart_family(products_world, auth_db)
    response = await _get(api, products_world, listing_id, "owner@bharat.test")
    assert response.status_code == 404, response.text


async def test_listings_preview_no_socket_no_account(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    """No marketplace account exists and only loopback is allowed: the preview sends nothing."""
    listing_id, _ = _standalone(products_world, auth_db)
    row = auth_db.execute("SELECT count(*) FROM marketplace_accounts").fetchone()
    assert row == (0,)
    response = await _get(api, products_world, listing_id)
    assert response.status_code == 200, response.text


async def test_listings_preview_no_token_or_url_in_body(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    listing_id, _ = _standalone(products_world, auth_db)
    response = await _get(api, products_world, listing_id)
    text = response.text
    assert "http://" not in text and "https://" not in text
    for marker in ("Atza|", "Atzr|", "Bearer", "eyJ", "access_token", "refresh_token"):
        assert marker not in text
    assert "passed" not in json.loads(text)


# ---- DW2 T10 (AC8, AC10, AC13): the stored Amazon definition in the preview ----


def _ptd_kurta(**changes: Any) -> dict[str, Any]:
    from tests.support.fake_marketplace import ptd_fixture

    schema: dict[str, Any] = json.loads(ptd_fixture("KURTA.schema.json"))
    value = schema["properties"]
    value["item_name"]["items"]["properties"]["value"]["maxLength"] = changes.get("title_max", 200)
    if "neck_enum" in changes:
        value["neck_style"]["items"]["properties"]["value"]["enum"] = changes["neck_enum"]
    if "keyword_bytes" in changes:
        keyword = value["generic_keyword"]["items"]["properties"]["value"]
        keyword["maxUtf8ByteLength"] = changes["keyword_bytes"]
    schema["required"] = [*schema["required"], *changes.get("required", ())]
    return schema


def _statements() -> tuple[list[str], Any]:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    seen: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", capture)
    return seen, lambda: event.remove(Engine, "before_cursor_execute", capture)


async def test_listings_preview_checks_the_stored_definition(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    from app.adapters.amazon import schema_check

    from .ptd_support import store

    schema_check.clear_cache()
    store(
        auth_db,
        _ptd_kurta(title_max=10, neck_enum=["Boat Neck"], keyword_bytes=5, required=["brand"]),
    )
    image_id = uuid.uuid4()
    body = _body(image_id, neckline="round")
    listing_id, _ = _standalone(products_world, auth_db, content=body)
    response = await _get(api, products_world, listing_id)
    assert response.status_code == 200, response.text
    report = response.json()["report"]
    assert report["status"] == "failed"
    found = {(e["field"], e["error_code"]) for e in report["errors"]}
    assert found == {
        ("attributes.fit", "MISSING_ATTRIBUTE"),
        ("listing", "MISSING_ATTRIBUTE"),
        ("content.title", "FIELD_TOO_LONG"),
        ("attributes.neckline", "VALUE_NOT_ALLOWED"),
        ("keywords.search_terms", "BYTE_BUDGET_EXCEEDED"),
        ("keywords.hidden_terms", "BYTE_BUDGET_EXCEEDED"),
    }
    for error in report["errors"]:
        assert error["value_submitted"] is None
        assert error["raw_code"].endswith(":UFAKE0001")
        for value in ("Navy cotton kurta set", "round", "kurta set", "cotton kurta"):
            assert value not in error["message"]


async def test_preview_with_a_deep_stored_schema_fails_closed(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    """Review round 1: a deeply nested stored definition
    (600 nested objects, accepted by `templates refresh`) is one SCHEMA_INVALID on `listing`,
    never a 500 (PLAN D5 fails closed, AC10)."""
    from app.adapters.amazon import schema_check

    from .ptd_support import store

    schema_check.clear_cache()
    depth = 300
    deep = json.loads(('{"properties":{"a":' * depth) + "{}" + ("}}" * depth))
    store(auth_db, {"type": "object", **deep})
    listing_id, _ = _standalone(products_world, auth_db)
    response = await _get(api, products_world, listing_id)
    assert response.status_code == 200, response.status_code
    codes = {(e["field"], e["error_code"]) for e in response.json()["report"]["errors"]}
    assert ("listing", "SCHEMA_INVALID") in codes


async def test_listings_preview_unchanged_without_a_definition(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    from .ptd_support import store

    listing_id, _ = _standalone(products_world, auth_db)
    before = (await _get(api, products_world, listing_id)).content
    # a definition for another product type is never this listing's
    store(auth_db, {"type": "object", "required": ["brand"]}, product_type="SHIRT")
    after = (await _get(api, products_world, listing_id)).content
    assert after == before


async def test_listings_preview_reads_the_schema_once_per_digest(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    from app.adapters.amazon import schema_check

    from .ptd_support import store

    schema_check.clear_cache()
    store(auth_db, _ptd_kurta(title_max=150))
    listing_id, _ = _standalone(products_world, auth_db)
    seen, stop = _statements()
    try:
        first = await _get(api, products_world, listing_id)
        reads_first = [s for s in seen if "schema_json" in s]
        seen.clear()
        second = await _get(api, products_world, listing_id)
        reads_second = [s for s in seen if "schema_json" in s]
        touched = [s for s in seen if "product_type_definitions" in s]
    finally:
        stop()
    assert first.status_code == second.status_code == 200
    assert len(reads_first) == 1 and reads_second == []
    assert len(touched) == 1  # the light `current()` read only
