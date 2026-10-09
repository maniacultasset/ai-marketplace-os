"""S19 T5 (AC 12, 15): `GET /api/v1/listings`, the review queue, and its case matrix."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .products_support import bad_bearer, bearer_for
from .review_support import REVIEWER, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
URL = "/api/v1/listings"
ROLES = ("owner", "operator", "reviewer", "admin")
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _stamp(world: GenWorld, listing_id: uuid.UUID, at: datetime) -> None:
    with world.conn.transaction():
        world.conn.execute("SET LOCAL session_replication_role = replica")
        world.conn.execute(
            "UPDATE listings SET status_changed_at = %s, last_event_at = %s WHERE id = %s",
            [at, at, listing_id],
        )


async def _get(
    api: httpx.AsyncClient, world: GenWorld, email: str = REVIEWER, **params: object
) -> httpx.Response:
    return await api.get(URL, headers=bearer_for(world, email), params=params)  # type: ignore[arg-type]


async def _queue(world: GenWorld, n: int = 3) -> list[uuid.UUID]:
    ids = []
    for i in range(n):
        made = await generated(world)
        _stamp(world, made.listing_id, T0 + timedelta(minutes=i))
        ids.append(made.listing_id)
    return ids


@pytest.mark.endpoint("listings_list", case="success")
async def test_listings_list_queue_order(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    ids = await _queue(gen_world)
    response = await _get(api, gen_world, status="validated")
    assert response.status_code == 200, response.text
    body = response.json()
    got = [uuid.UUID(row["id"]) for row in body["data"]]
    assert [i for i in got if i in ids] == ids  # oldest first
    assert all(row["status"] == "validated" for row in body["data"])
    assert "content" not in body["data"][0]


async def test_listings_list_two_pages(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    ids = await _queue(gen_world, 3)
    first = (await _get(api, gen_world, status="validated", limit=2)).json()
    assert first["page"]["has_more"] is True and first["page"]["limit"] == 2
    second = await _get(
        api, gen_world, status="validated", limit=2, cursor=first["page"]["next_cursor"]
    )
    assert second.status_code == 200, second.text
    seen = [uuid.UUID(r["id"]) for r in first["data"] + second.json()["data"]]
    assert len(seen) == len(set(seen))
    assert set(ids) <= set(seen)


async def test_listings_list_filters(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    a, b = await generated(gen_world), await generated(gen_world)
    set_status(gen_world, b.listing_id, "needs_review")
    product = gen_world.listing(a.listing_id)["product_id"]
    body = (await _get(api, gen_world, product_id=str(product))).json()
    assert [r["id"] for r in body["data"]] == [str(a.listing_id)]
    body = (await _get(api, gen_world, status="needs_review", marketplace="amazon")).json()
    assert str(b.listing_id) in {r["id"] for r in body["data"]}
    assert str(a.listing_id) not in {r["id"] for r in body["data"]}
    body = (await _get(api, gen_world, product_id=str(uuid.uuid4()))).json()
    assert body["data"] == [] and body["page"]["has_more"] is False


@pytest.mark.endpoint("listings_list", case="validation")
@pytest.mark.parametrize(
    "params",
    [
        {"status": "bogus"},
        {"marketplace": "ebay"},
        {"product_id": "not-a-uuid"},
        {"limit": 0},
        {"limit": 201},
        {"cursor": "!!!"},
        {"cursor": "eyJ0IjoxfQ"},
    ],
)
async def test_listings_list_bad_query_422(
    api: httpx.AsyncClient, gen_world: GenWorld, params: dict[str, object]
) -> None:
    response = await _get(api, gen_world, REVIEWER, **params)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


async def test_listings_list_cursor_other_order_422(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _queue(gen_world, 3)
    recent = (await _get(api, gen_world, limit=1)).json()
    assert recent["page"]["next_cursor"]
    response = await _get(
        api, gen_world, status="validated", limit=1, cursor=recent["page"]["next_cursor"]
    )
    assert response.status_code == 422
    from app.api.pagination import encode_cursor

    plain = encode_cursor(T0, uuid.uuid4())  # an untagged cursor (another list's) is refused too
    assert (await _get(api, gen_world, cursor=plain)).status_code == 422


@pytest.mark.endpoint("listings_list", case="missing_auth")
async def test_listings_list_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.get(URL)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    response = await api.get(URL, headers=bad_bearer(world, variant))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("listings_list", case="invalid_token", variant="expired")
async def test_listings_list_invalid_token_expired(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("listings_list", case="invalid_token", variant="bad_signature")
async def test_listings_list_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("listings_list", case="invalid_token", variant="wrong_typ")
async def test_listings_list_invalid_token_wrong_typ(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "wrong_typ")


@pytest.mark.endpoint("listings_list", case="wrong_tenant")
async def test_listings_list_bharat_sees_no_acme(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    ids = await _queue(gen_world, 2)
    body = (await _get(api, gen_world, "reviewer@bharat.test")).json()
    assert not {str(i) for i in ids} & {r["id"] for r in body["data"]}
    acme_tenant = gen_world.tenant("acme")
    acme_listings = {
        str(r[0])
        for r in gen_world.conn.execute(
            "SELECT id FROM listings WHERE tenant_id = %s", [acme_tenant]
        ).fetchall()
    }
    assert not acme_listings & {r["id"] for r in body["data"]}


@pytest.mark.parametrize("role", ROLES)
async def test_listings_list_all_roles_200(
    api: httpx.AsyncClient, gen_world: GenWorld, role: str
) -> None:
    """n/a wrong_role: every role holds tenant:read."""
    assert (await _get(api, gen_world, f"{role}@acme.test")).status_code == 200
