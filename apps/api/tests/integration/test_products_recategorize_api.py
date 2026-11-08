"""SI9 T15: `POST /api/v1/products/{product_id}/recategorize` as app_rw through the real app. The
category fix for a product that already has listings: its unpublished listings take the new
category and wait for a regenerate; a listing the marketplace has seen is never touched."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .products_support import bad_bearer
from .review_support import OPERATOR, REVIEWER, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
NEW = "tshirts"
OLD = "ethnic_wear_kurta_sets"


def _url(product_id: object) -> str:
    return f"/api/v1/products/{product_id}/recategorize"


async def _post(
    api: httpx.AsyncClient,
    world: GenWorld,
    product_id: object,
    body: Any,
    email: str = OPERATOR,
) -> httpx.Response:
    return await api.post(_url(product_id), headers=world.headers(email), json=body)


async def _made(world: GenWorld) -> tuple[uuid.UUID, uuid.UUID]:
    made = await generated(world)
    product = world.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id])
    return uuid.UUID(str(product)), made.listing_id


def _category(world: GenWorld, product_id: uuid.UUID) -> str:
    return str(world.scalar("SELECT category FROM products WHERE id = %s", [product_id]))


# ---- the case matrix ----


@pytest.mark.endpoint("products_recategorize", case="success")
async def test_recategorize_supersedes_the_listing(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product, listing_id = await _made(gen_world)
    set_status(gen_world, listing_id, "approved")
    with gen_world.conn.transaction():
        gen_world.conn.execute("SET LOCAL session_replication_role = replica")
        gen_world.conn.execute(
            "UPDATE listings SET approved_version_id = current_version_id WHERE id = %s",
            [listing_id],
        )
    response = await _post(api, gen_world, product, {"category": NEW})
    assert response.status_code == 200, response.text
    assert response.json()["category"] == NEW and response.headers["ETag"]
    assert _category(gen_world, product) == NEW
    row = gen_world.listing(listing_id)
    assert (row["category"], row["status"], row["status_reason"], row["approved_version_id"]) == (
        NEW,
        "blocked",
        "validation_errors",
        None,
    )
    audits = gen_world.conn.execute(
        "SELECT before, after, reason FROM audit_log WHERE entity_id = %s AND action = %s",
        [product, "product.recategorized"],
    ).fetchall()
    assert [
        (b["provenance"]["category"]["value"], a["provenance"]["category"]["value"], r)
        for b, a, r in audits
    ] == [(OLD, NEW, f"listings:{listing_id}")]


@pytest.mark.endpoint("products_recategorize", case="validation")
@pytest.mark.parametrize("body", [{}, {"category": ""}, {"category": "Not A Slug"}, {"x": 1}])
async def test_recategorize_validation_422(
    api: httpx.AsyncClient, gen_world: GenWorld, body: Any
) -> None:
    product, _ = await _made(gen_world)
    response = await _post(api, gen_world, product, body)
    assert response.status_code == 422, response.text
    assert _category(gen_world, product) == OLD


async def test_recategorize_unmapped_category_422(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product, listing_id = await _made(gen_world)
    response = await _post(api, gen_world, product, {"category": "no_such_category"})
    assert response.status_code == 422, response.text
    assert _category(gen_world, product) == OLD
    assert gen_world.listing(listing_id)["category"] == OLD


@pytest.mark.endpoint("products_recategorize", case="missing_auth")
async def test_recategorize_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.post(_url(uuid.uuid4()), json={"category": NEW})
    assert response.status_code == 401


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    response = await api.post(
        _url(uuid.uuid4()), headers=bad_bearer(world, variant, OPERATOR), json={"category": NEW}
    )
    assert response.status_code == 401


@pytest.mark.endpoint("products_recategorize", case="invalid_token", variant="expired")
async def test_recategorize_invalid_token_expired(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("products_recategorize", case="invalid_token", variant="bad_signature")
async def test_recategorize_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("products_recategorize", case="invalid_token", variant="wrong_typ")
async def test_recategorize_invalid_token_wrong_typ(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "wrong_typ")


@pytest.mark.endpoint("products_recategorize", case="wrong_role")
async def test_recategorize_wrong_role_403(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    product, _ = await _made(gen_world)
    response = await _post(api, gen_world, product, {"category": NEW}, email=REVIEWER)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _category(gen_world, product) == OLD


@pytest.mark.endpoint("products_recategorize", case="wrong_tenant")
async def test_recategorize_foreign_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    product, _ = await _made(gen_world)
    foreign = await _post(api, gen_world, product, {"category": NEW}, "operator@bharat.test")
    missing = await _post(api, gen_world, uuid.uuid4(), {"category": NEW}, "operator@bharat.test")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert _category(gen_world, product) == OLD


@pytest.mark.endpoint("products_recategorize", case="not_found")
async def test_recategorize_not_found_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _post(api, gen_world, uuid.uuid4(), {"category": NEW})
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


@pytest.mark.endpoint("products_recategorize", case="conflict")
@pytest.mark.parametrize("status", ["published", "publishing"])
async def test_recategorize_seen_listing_409(
    api: httpx.AsyncClient, gen_world: GenWorld, status: str
) -> None:
    product, listing_id = await _made(gen_world)
    set_status(gen_world, listing_id, status)
    response = await _post(api, gen_world, product, {"category": NEW})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    assert _category(gen_world, product) == OLD
    assert gen_world.listing(listing_id)["category"] == OLD


# ---- the rest ----


async def test_recategorize_generating_listing_409_busy(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product, listing_id = await _made(gen_world)
    set_status(gen_world, listing_id, "generating")
    response = await _post(api, gen_world, product, {"category": NEW})
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "LISTING_BUSY"


async def test_recategorize_same_category_writes_nothing(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product, listing_id = await _made(gen_world)
    before = gen_world.listing(listing_id)
    response = await _post(api, gen_world, product, {"category": OLD})
    assert response.status_code == 200, response.text
    after = gen_world.listing(listing_id)
    assert (after["status"], after["updated_at"]) == (before["status"], before["updated_at"])
    assert (
        gen_world.count(
            "audit_log", "entity_id = %s AND action = 'product.recategorized'", [product]
        )
        == 0
    )


async def test_regenerate_after_recategorize_uses_the_new_category(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product, listing_id = await _made(gen_world)
    assert (await _post(api, gen_world, product, {"category": NEW})).status_code == 200
    response = await api.post(
        f"/api/v1/listings/{listing_id}/regenerate",
        headers=gen_world.headers(OPERATOR),
        json={},
    )
    assert response.status_code == 202, response.text
    assert gen_world.listing(listing_id)["status"] == "generating"
    assert gen_world.listing(listing_id)["category"] == NEW
