"""S21 fix round 1: a newer version after an older one reached the marketplace, an older
version's open `pending` row, and the failed-fields payload after a partial (invariants 1, 10;
D8; CP1; L12033).

- A partial on v1 puts part of the listing on the marketplace: v2's attempt 1 is an `update`,
  never a `create` that D8's read-before-retry would wrongly find "applied" through v1's SKUs.
- A dead-lettered v1 `pending` row is never resent with v2's content nor resolved into v2's
  ledger: it is resolved by a read (found → `accepted` then `superseded`), and v2 is sent.
- After a `partially_accepted` attempt, `resume` and a `publish` of `failed/publish_failed`
  resend only the failed fields, never the whole listing.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.adapters.base import MarketplaceAmbiguous, MarketplaceUnavailable
from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, pub, publish_and_run, publish_one, run_job
from .review_support import OWNER, REVIEWER, Made, edited

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]
RL = ("Throttled", "Request was throttled", ErrorCode.RATE_LIMITED)


def _report() -> ErrorReport:
    return ErrorReport(status="failed")


async def _run_tolerant(job_id: Any) -> str | None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:
        assert type(exc).__name__ == "RetryLater", exc
        return "retry"
    return None


async def _approve(pw: PubWorld, made: Made) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.review import ReviewItem, approve_batch

    principal = pw.world.principal(REVIEWER)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        outcome = await approve_batch(
            uow,
            principal,
            [ReviewItem(made.listing_id, made.version_id, "approve")],
            http_key=str(uuid.uuid4()),
        )
    assert outcome.items[0].http_status == 200, outcome.items[0]


async def _new_version(api: httpx.AsyncClient, pub: PubWorld, made: Made) -> Made:
    v2 = await edited(api, pub.world, made, email=OWNER)
    await _approve(pub, v2)
    return v2


async def _partial_then_v2(api: httpx.AsyncClient, pub: PubWorld) -> Made:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    assert pub.listing(made.listing_id)["status_reason"] == "publish_partial"
    pub.fake.set_fail_fields({})
    return await _new_version(api, pub, made)


def _sku(pub: PubWorld, listing_id: uuid.UUID) -> str:
    return str(
        pub.scalar(
            "SELECT seller_sku FROM listing_items WHERE listing_id = %s LIMIT 1", [listing_id]
        )
    )


# ---- finding 1: an older version's partial makes the next version an update ----


async def test_new_version_after_partial_is_update(api: httpx.AsyncClient, pub: PubWorld) -> None:
    v2 = await _partial_then_v2(api, pub)
    await publish_and_run(api, pub, v2)
    sub = pub.submissions(v2.listing_id)[-1]
    assert (sub["listing_version_id"], sub["operation"]) == (v2.version_id, "update")


async def test_ambiguous_v2_after_partial_is_resent_not_read_as_applied(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """The v2 send fails before the marketplace writes anything: the next run resends the update
    on the same row and key; v1's SKUs never make the unsent v2 look applied."""
    v2 = await _partial_then_v2(api, pub)
    sku = _sku(pub, v2.listing_id)
    before = pub.fake.stored(sku)
    pub.fake.raise_next = [MarketplaceAmbiguous(_report())]
    item = await publish_one(api, pub, v2)
    for _ in range(2):
        await _run_tolerant(item["job_id"])
    sub = pub.submissions(v2.listing_id)[-1]
    assert (sub["operation"], sub["outcome"]) == ("update", "accepted")
    assert pub.fake.stored(sku) != before  # v2 was really applied
    assert pub.fake.calls[-1][0] == "update"


# ---- finding 2: an older version's open pending row ----


async def _v1_pending_dead_letter(api: httpx.AsyncClient, pub: PubWorld, *, applied: bool) -> Made:
    """v1's create is applied (or not) but the answer is lost; the job dead-letters with the
    row `pending` and the listing `failed/publish_failed`."""
    made = await approved(pub)
    reads = pub.fake.get_listing

    async def unreadable(*a: Any, **k: Any) -> Any:
        raise MarketplaceUnavailable(_report())

    if applied:
        pub.fake.raise_after_write = MarketplaceAmbiguous(_report())
    else:
        pub.fake.raise_next = [MarketplaceAmbiguous(_report())]
    pub.fake.get_listing = unreadable
    item = await publish_one(api, pub, made)
    for _ in range(3):
        await _run_tolerant(item["job_id"])
    assert pub.job(item["job_id"])["status"] == "dead_letter"
    pub.fake.get_listing = reads
    pub.fake.raise_next = []
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "publish_failed")
    (sub,) = pub.submissions(made.listing_id)
    assert (sub["outcome"], sub["operation"]) == ("pending", "create")
    return made


async def test_foreign_pending_found_is_superseded_and_v2_sent(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await _v1_pending_dead_letter(api, pub, applied=True)
    v2 = await _new_version(api, pub, made)
    calls = len(pub.fake.calls)
    again = await publish_one(api, pub, v2)
    assert again["http_status"] == 202, again
    assert await _run_tolerant(again["job_id"]) is None
    subs = pub.submissions(made.listing_id)
    assert [
        (s["listing_version_id"], s["attempt"], s["outcome"], s["operation"]) for s in subs
    ] == [
        (made.version_id, 1, "superseded", "create"),
        (v2.version_id, 1, "accepted", "update"),
    ]
    # v2 really went out (one update), and no field result of v2 sits on v1's row
    assert [c[0] for c in pub.fake.calls[calls:]] == ["update"]
    assert (
        pub.scalar(
            "SELECT count(*) FROM publish_field_results WHERE submission_id = %s",
            [subs[0]["id"]],
        )
        == 0
    )
    row = pub.listing(made.listing_id)
    assert (row["status"], row["published_version_id"]) == ("published", v2.version_id)


async def test_foreign_pending_not_found_is_not_applied_and_v2_created(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await _v1_pending_dead_letter(api, pub, applied=False)
    v2 = await _new_version(api, pub, made)
    calls = len(pub.fake.calls)
    again = await publish_one(api, pub, v2)
    assert await _run_tolerant(again["job_id"]) is None
    subs = pub.submissions(made.listing_id)
    assert [
        (s["listing_version_id"], s["attempt"], s["outcome"], s["error_code"], s["operation"])
        for s in subs
    ] == [
        (made.version_id, 1, "error", "SUBMISSION_NOT_APPLIED", "create"),
        (v2.version_id, 1, "accepted", None, "create"),
    ]
    assert [c[0] for c in pub.fake.calls[calls:]] == ["create"]
    assert pub.listing(made.listing_id)["published_version_id"] == v2.version_id
