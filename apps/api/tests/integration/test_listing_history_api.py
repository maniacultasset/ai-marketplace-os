"""S19 T7 (AC 12, 15): `GET /api/v1/listings/{listing_id}/history` and its case matrix."""

from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bad_bearer, bearer_for
from .review_support import REVIEWER, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
URL = "/api/v1/listings"


def _audit(world: GenWorld, listing_id: uuid.UUID, n: int, slug: str = "acme") -> None:
    """`n` rows for the listing with an actor email and an IP set (admin login)."""
    for i in range(n):
        world.conn.execute(
            "INSERT INTO audit_log (tenant_id, actor_type, actor_user_id, actor_email, action, "
            "entity_type, entity_id, after, ip, occurred_at) VALUES (%s, 'user', %s, "
            "'someone@acme.test', %s, 'listing', %s, '{\"status\": \"approved\"}', "
            "'198.51.100.9', '2026-10-01T09:00:00Z'::timestamptz + make_interval(secs => %s))",
            [world.tenant(slug), world.user(REVIEWER), f"listing.test_{i}", listing_id, i],
        )


async def _get(
    api: httpx.AsyncClient,
    world: GenWorld,
    listing_id: object,
    email: str = REVIEWER,
    **params: object,
) -> httpx.Response:
    return await api.get(
        f"{URL}/{listing_id}/history", headers=bearer_for(world, email), params=params  # type: ignore[arg-type]
    )


@pytest.mark.endpoint("listings_history", case="success")
async def test_listing_history_newest_first_keyset(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    _audit(gen_world, made.listing_id, 3)
    first = await _get(api, gen_world, made.listing_id, limit=2)
    assert first.status_code == 200, first.text
    body = first.json()
    assert [e["action"] for e in body["data"]] == ["listing.test_2", "listing.test_1"]
    assert body["page"]["has_more"] is True
    second = (
        await _get(api, gen_world, made.listing_id, limit=2, cursor=body["page"]["next_cursor"])
    ).json()
    assert [e["action"] for e in second["data"]] == ["listing.test_0"]
    assert second["page"]["has_more"] is False


async def test_listing_history_no_email_no_ip(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    _audit(gen_world, made.listing_id, 1)
    response = await _get(api, gen_world, made.listing_id)
    assert "someone@acme.test" not in response.text and "198.51.100.9" not in response.text
    event = response.json()["data"][0]
    assert "actor_email" not in event and "ip" not in event
    assert event["after"] == {"status": "approved"}


async def test_listing_history_only_this_listing(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    a, b = await generated(gen_world), await generated(gen_world)
    _audit(gen_world, a.listing_id, 2)
    _audit(gen_world, b.listing_id, 1)
    body = (await _get(api, gen_world, b.listing_id)).json()
    assert len(body["data"]) == 1
    assert all(e["entity_id"] == str(b.listing_id) for e in body["data"])


@pytest.mark.endpoint("listings_history", case="validation")
@pytest.mark.parametrize(
    "bad", [("not-a-uuid", {}), (None, {"limit": 0}), (None, {"cursor": "%%"})]
)
async def test_listing_history_validation_422(
    api: httpx.AsyncClient, gen_world: GenWorld, bad: tuple[str | None, dict[str, object]]
) -> None:
    listing, params = bad
    response = await _get(api, gen_world, listing or uuid.uuid4(), REVIEWER, **params)
    assert response.status_code == 422


@pytest.mark.endpoint("listings_history", case="missing_auth")
async def test_listing_history_missing_auth_401(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    assert (await api.get(f"{URL}/{uuid.uuid4()}/history")).status_code == 401


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    response = await api.get(f"{URL}/{uuid.uuid4()}/history", headers=bad_bearer(world, variant))
    assert response.status_code == 401


@pytest.mark.endpoint("listings_history", case="invalid_token", variant="expired")
async def test_listing_history_invalid_token_expired(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("listings_history", case="invalid_token", variant="bad_signature")
async def test_listing_history_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("listings_history", case="invalid_token", variant="wrong_typ")
async def test_listing_history_invalid_token_wrong_typ(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "wrong_typ")


@pytest.mark.endpoint("listings_history", case="wrong_tenant")
async def test_listing_history_foreign_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    _audit(gen_world, made.listing_id, 1)
    headers = {**bearer_for(gen_world, "reviewer@bharat.test"), "X-Request-ID": "req-s19-h-0001"}
    foreign = await api.get(f"{URL}/{made.listing_id}/history", headers=headers)
    missing = await api.get(f"{URL}/{uuid.uuid4()}/history", headers=headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.content == missing.content


@pytest.mark.endpoint("listings_history", case="not_found")
async def test_listing_history_not_found_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _get(api, gen_world, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"
