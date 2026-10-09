"""S19 T12 (AC 7, 11; invariant 10; T42; DATABASE.md §7.1): partial batches, the bulk backstop,
idempotency, overlapping batches and races on the listing row lock."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .products_support import bearer_for
from .review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    Made,
    decisions,
    generated,
    item,
    post_approve,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


def _codes(response: httpx.Response) -> list[tuple[int, str | None]]:
    assert response.status_code == 200, response.text
    return [
        (i["http_status"], None if i["error"] is None else i["error"]["error_code"])
        for i in response.json()["items"]
    ]


async def test_approve_batch_partial(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    ok, review = await generated(gen_world), await generated(gen_world)
    set_status(gen_world, review.listing_id, "needs_review", None)
    foreign = await generated(gen_world, "operator@bharat.test")
    response = await post_approve(api, gen_world, REVIEWER, [item(ok), item(review), item(foreign)])
    assert _codes(response) == [
        (200, None),
        (409, "BULK_APPROVE_REQUIRES_VALIDATED"),
        (404, "NOT_FOUND"),
    ]
    assert gen_world.listing(ok.listing_id)["status"] == "approved"
    assert gen_world.listing(review.listing_id)["status"] == "needs_review"
    assert gen_world.listing(foreign.listing_id)["status"] == "validated"
    count = gen_world.scalar(
        "SELECT item_count FROM batches WHERE id = %s", [response.json()["batch_id"]]
    )
    assert count == 3


async def test_needs_review_single_item_approves(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    set_status(gen_world, made.listing_id, "needs_review", None)
    assert _codes(await post_approve(api, gen_world, REVIEWER, [item(made)])) == [(200, None)]


async def test_bulk_backstop_rev01(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API's bulk check patched out: the database's review backstop refuses (REV01)."""
    import app.services.review as review

    monkeypatch.setattr(review, "bulk_check", lambda *_a: None)
    ok, needs = await generated(gen_world), await generated(gen_world)
    set_status(gen_world, needs.listing_id, "needs_review", None)
    response = await post_approve(api, gen_world, REVIEWER, [item(ok), item(needs)])
    assert _codes(response) == [(200, None), (409, "BULK_APPROVE_REQUIRES_VALIDATED")]
    assert decisions(gen_world, needs.listing_id) == 0
    assert (
        gen_world.scalar(
            "SELECT approved_by_user_id FROM listing_versions WHERE id = %s", [needs.version_id]
        )
        is None
    )


async def test_approve_idempotency(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    a, b = await generated(gen_world), await generated(gen_world)
    key = str(uuid.uuid4())
    first = await post_approve(api, gen_world, REVIEWER, [item(a)], key=key)
    rows = (gen_world.count("review_decisions"), gen_world.count("batches"))
    again = await post_approve(api, gen_world, REVIEWER, [item(a)], key=key)
    assert again.status_code == 200
    assert again.content == first.content
    assert (gen_world.count("review_decisions"), gen_world.count("batches")) == rows
    other = await post_approve(api, gen_world, REVIEWER, [item(b)], key=key)
    assert other.status_code == 409
    assert other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    headers = bearer_for(gen_world, REVIEWER)
    missing = await api.post("/api/v1/listings/approve", headers=headers, json={"items": [item(a)]})
    assert missing.status_code == 422


async def test_approve_retry_only_failed_items(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    a, b = await generated(gen_world), await generated(gen_world)
    stale_b = {**item(b), "listing_version_id": str(uuid.uuid4())}
    first = await post_approve(api, gen_world, REVIEWER, [item(a), stale_b])
    assert _codes(first) == [(200, None), (409, "STALE_VERSION")]
    approved_at = gen_world.scalar(
        "SELECT approved_at FROM listing_versions WHERE id = %s", [a.version_id]
    )
    retry = await post_approve(api, gen_world, REVIEWER, [item(b)])
    assert _codes(retry) == [(200, None)]
    assert (
        gen_world.scalar("SELECT approved_at FROM listing_versions WHERE id = %s", [a.version_id])
        == approved_at
    )
    assert decisions(gen_world, a.listing_id) == 1 and decisions(gen_world, b.listing_id) == 1


async def test_approve_concurrent_batches(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    """`[L1, L2]` and `[L2, L1]` at once: id-ordered locks, so both finish, no deadlock, and
    each listing is approved exactly once."""
    one, two = await generated(gen_world), await generated(gen_world)
    first, second = await asyncio.gather(
        post_approve(api, gen_world, REVIEWER, [item(one), item(two)]),
        post_approve(api, gen_world, OWNER, [item(two), item(one)]),
    )
    results = _codes(first) + _codes(second)
    assert sorted(r[0] for r in results) == [200, 200, 409, 409]
    for made in (one, two):
        assert gen_world.listing(made.listing_id)["status"] == "approved"
        assert decisions(gen_world, made.listing_id) == 1


async def _patch(api: httpx.AsyncClient, world: GenWorld, made: Made) -> httpx.Response:
    headers = {**bearer_for(world, OPERATOR), "If-Match": f'"{made.version_id}"'}
    return await api.patch(
        f"/api/v1/listings/{made.listing_id}/content",
        headers=headers,
        json={
            "content": {
                "title": "Cotton Kurta Set for Women",
                "bullets": ["Soft cotton fabric"],
                "description": "A cotton kurta set with a straight fit.",
            }
        },
    )


async def _regenerate(api: httpx.AsyncClient, world: GenWorld, made: Made) -> httpx.Response:
    headers = {**bearer_for(world, OPERATOR), "Idempotency-Key": str(uuid.uuid4())}
    return await api.post(f"/api/v1/listings/{made.listing_id}/regenerate", headers=headers)


def _consistent(world: GenWorld, made: Made) -> None:
    """Whatever won the row lock: approved ⇒ the approved version is current and its approver
    is outside the chain (here: not the operator, who generated and edited)."""
    row = world.listing(made.listing_id)
    if row["status"] == "approved":
        assert row["approved_version_id"] == row["current_version_id"]
        approver = world.scalar(
            "SELECT approved_by_user_id FROM listing_versions WHERE id = %s",
            [row["current_version_id"]],
        )
        assert approver == world.user(REVIEWER)
    else:
        assert row["approved_version_id"] is None


@pytest.mark.parametrize("other", ["patch", "regenerate"])
async def test_review_races(api: httpx.AsyncClient, gen_world: GenWorld, other: str) -> None:
    for _ in range(3):
        made = await generated(gen_world, OPERATOR)
        racer: Any = _patch if other == "patch" else _regenerate
        approve, competing = await asyncio.gather(
            post_approve(api, gen_world, REVIEWER, [item(made)]),
            racer(api, gen_world, made),
        )
        assert approve.status_code == 200
        assert competing.status_code in (200, 202, 409), competing.text
        _consistent(gen_world, made)
