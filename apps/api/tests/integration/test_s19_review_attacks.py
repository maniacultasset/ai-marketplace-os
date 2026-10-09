"""S19 tester attacks (round 1): idempotency keys across issues and routes, duplicate listings in
one batch, two approvers racing on one listing, feedback that must never reach the history, and
oversized or multi-byte feedback. Every probe runs API -> service -> repository -> Postgres."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bearer_for
from .review_support import (
    OWNER,
    REVIEWER,
    audits,
    decisions,
    generated,
    item,
    only_item,
    open_issue,
    post_approve,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


async def _dismiss(
    api: httpx.AsyncClient, world: GenWorld, issue: uuid.UUID, key: str
) -> httpx.Response:
    headers = {**bearer_for(world, REVIEWER), "Idempotency-Key": key}
    return await api.post(f"/api/v1/issues/{issue}/dismiss", headers=headers)


def _issue_status(world: GenWorld, issue: uuid.UUID) -> str:
    return str(world.scalar("SELECT status FROM listing_issues WHERE id = %s", [issue]))


async def test_dismiss_same_key_other_issue_409_leaves_it_open(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """A replayed key must never hand issue A's stored body back for issue B."""
    made = await generated(gen_world)
    first = await open_issue(gen_world, made)
    second = await open_issue(gen_world, made, field_path="content.description")
    key = str(uuid.uuid4())
    assert (await _dismiss(api, gen_world, first, key)).status_code == 200
    again = await _dismiss(api, gen_world, second, key)
    assert again.status_code == 409, again.text
    assert again.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    assert _issue_status(gen_world, second) == "open"


async def test_dismiss_key_reused_from_approve_409(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    issue = await open_issue(gen_world, made)
    key = str(uuid.uuid4())
    assert (
        only_item(await post_approve(api, gen_world, REVIEWER, [item(made)], key=key))[
            "http_status"
        ]
        == 200
    )
    reused = await _dismiss(api, gen_world, issue, key)
    assert reused.status_code == 409, reused.text
    assert reused.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    assert _issue_status(gen_world, issue) == "open"


@pytest.mark.parametrize("second", ["approve", "reject"])
async def test_approve_same_listing_twice_in_one_batch_422_no_rows(
    api: httpx.AsyncClient, gen_world: GenWorld, second: str
) -> None:
    made = await generated(gen_world)
    extra = {"reason_code": "poor_quality", "feedback": "Too long."} if second == "reject" else {}
    before = gen_world.count("batches", "created_by_user_id = %s", [gen_world.user(REVIEWER)])
    response = await post_approve(
        api, gen_world, REVIEWER, [item(made), item(made, second, **extra)]
    )
    assert response.status_code == 422, response.text
    after = gen_world.count("batches", "created_by_user_id = %s", [gen_world.user(REVIEWER)])
    assert after == before
    assert gen_world.listing(made.listing_id)["status"] == "validated"
    assert decisions(gen_world, made.listing_id) == 0


async def test_two_approvers_race_one_listing_exactly_one_approval(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """Reviewer and owner (both outside the chain) approve the same version at once with their
    own keys: the row lock lets one win; the other gets an item 409 and writes nothing."""
    for _ in range(3):
        made = await generated(gen_world)
        first, second = await asyncio.gather(
            post_approve(api, gen_world, REVIEWER, [item(made)]),
            post_approve(api, gen_world, OWNER, [item(made)]),
        )
        codes = sorted([only_item(first)["http_status"], only_item(second)["http_status"]])
        assert codes == [200, 409], (first.text, second.text)
        assert decisions(gen_world, made.listing_id) == 1
        assert audits(gen_world, made.listing_id, "listing.approved") == 1
        row = gen_world.listing(made.listing_id)
        assert (row["status"], row["approved_version_id"]) == ("approved", made.version_id)


async def test_reject_feedback_never_in_history(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """Feedback is untrusted free text: the audit carries the reason code only."""
    made = await generated(gen_world)
    marker = f"FEEDBACK-MARKER-{uuid.uuid4().hex}"
    body = item(made, "reject", reason_code="poor_quality", feedback=f"Fix it. {marker}")
    assert only_item(await post_approve(api, gen_world, REVIEWER, [body]))["http_status"] == 200
    history = await api.get(
        f"/api/v1/listings/{made.listing_id}/history", headers=bearer_for(gen_world, OWNER)
    )
    assert history.status_code == 200, history.text
    assert marker not in history.text
    rejected = [e for e in history.json()["data"] if e["action"] == "listing.rejected"]
    assert [e["reason"] for e in rejected] == ["poor_quality"]
    assert (
        gen_world.count(
            "audit_log", "entity_id = %s AND reason LIKE %s", [made.listing_id, f"%{marker}%"]
        )
        == 0
    )


async def test_reject_feedback_2000_multibyte_ok_2001_refused(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """The 2000 cap counts characters: 2000 four-byte characters are stored whole; 2001 is a
    request 422 with no decision row."""
    made = await generated(gen_world)
    too_long = item(made, "reject", reason_code="other", feedback="\U0001f600" * 2001)
    refused = await post_approve(api, gen_world, REVIEWER, [too_long])
    assert refused.status_code == 422, refused.text
    assert decisions(gen_world, made.listing_id, "reject") == 0
    fits = item(made, "reject", reason_code="other", feedback="\U0001f600" * 2000)
    assert only_item(await post_approve(api, gen_world, REVIEWER, [fits]))["http_status"] == 200
    stored = gen_world.scalar(
        "SELECT feedback FROM review_decisions WHERE listing_id = %s AND decision = 'reject'",
        [made.listing_id],
    )
    assert stored == "\U0001f600" * 2000
