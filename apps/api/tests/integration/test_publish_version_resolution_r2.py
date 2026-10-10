"""S21 re-check round 2: the fixes for an older version that reached the marketplace, an older
version's open `pending` row, and the failed-fields payload, attacked from other entry points
(invariants 1, 10; D8; CP1; L12033).

- After `blocked/publish_attempts_exhausted` whose attempts were all partials, an edited,
  re-approved v2 goes out as an `update`; an ambiguous v2 send that wrote nothing is resent on
  the same row and key, never read as applied through v1's SKUs.
- An older version's open `pending` *patch* (a field retry whose answer was lost) is never
  resent with v2's content: it becomes `error SUBMISSION_NOT_APPLIED` and v2 is sent as one
  full `update`, carrying no v1 field results.
- v2 after a partial on v1 is a full update of v2, never a patch of v1's failed fields; v2's own
  partial is then retried with only v2's failed field.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.adapters.base import MarketplaceAmbiguous
from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, pub, publish_and_run, publish_one, retry, run_job
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


def _sku(pub: PubWorld, listing_id: uuid.UUID) -> str:
    return str(
        pub.scalar(
            "SELECT seller_sku FROM listing_items WHERE listing_id = %s LIMIT 1", [listing_id]
        )
    )


async def test_v2_after_exhausted_partials_is_update_and_ambiguous_is_resent(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    for _ in range(2):
        response = await retry(api, pub, made)
        assert response.status_code == 202, response.text
        await run_job(uuid.UUID(response.json()["job_id"]))
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "publish_attempts_exhausted")
    assert row["published_version_id"] is None
    pub.fake.set_fail_fields({})
    v2 = await _new_version(api, pub, made)
    sku = _sku(pub, made.listing_id)
    before = pub.fake.stored(sku)
    pub.fake.raise_next = [MarketplaceAmbiguous(_report())]  # nothing written for v2
    item = await publish_one(api, pub, v2)
    for _ in range(2):
        await _run_tolerant(item["job_id"])
    subs = [s for s in pub.submissions(made.listing_id) if s["listing_version_id"] == v2.version_id]
    assert [(s["attempt"], s["operation"], s["outcome"]) for s in subs] == [
        (1, "update", "accepted")
    ]
    assert pub.fake.calls[-1][0] == "update"
    assert pub.fake.stored(sku) != before  # v2 really reached the marketplace
    row = pub.listing(made.listing_id)
    assert (row["status"], row["published_version_id"]) == ("published", v2.version_id)


async def test_foreign_pending_patch_is_not_applied_and_v2_full_update(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    pub.fake.set_fail_fields({})
    pub.fake.raise_next = [MarketplaceAmbiguous(_report()) for _ in range(3)]
    response = await retry(api, pub, made)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    for _ in range(3):
        await _run_tolerant(job_id)
    assert pub.job(job_id)["status"] == "dead_letter"
    pub.fake.raise_next = []
    subs = pub.submissions(made.listing_id)
    assert [(s["operation"], s["outcome"]) for s in subs] == [
        ("create", "partially_accepted"),
        ("patch", "pending"),
    ]
    v2 = await _new_version(api, pub, made)
    calls = len(pub.fake.calls)
    again = await publish_one(api, pub, v2)
    assert again["http_status"] == 202, again
    assert await _run_tolerant(again["job_id"]) is None
    subs = sorted(pub.submissions(made.listing_id), key=lambda s: s["created_at"])
    assert [
        (s["listing_version_id"], s["operation"], s["outcome"], s["error_code"]) for s in subs
    ] == [
        (made.version_id, "create", "partially_accepted", "API_REJECTED_PARTIAL"),
        (made.version_id, "patch", "error", "SUBMISSION_NOT_APPLIED"),
        (v2.version_id, "update", "accepted", None),
    ]
    # one full update of v2 (every field), never v1's patch resent
    sent = pub.fake.calls[calls:]
    assert [c[0] for c in sent] == ["update"]
    assert len(next(iter(sent[0][1].values()))) > 1
    assert (
        pub.scalar(
            "SELECT count(*) FROM publish_field_results WHERE submission_id = %s "
            "AND listing_version_id = %s",
            [subs[1]["id"], v2.version_id],
        )
        == 0
    )
    row = pub.listing(made.listing_id)
    assert (row["status"], row["published_version_id"]) == ("published", v2.version_id)


async def test_v2_after_v1_partial_is_full_update_then_retries_only_v2_failed(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    pub.fake.set_fail_fields({"content.description": RL})
    v2 = await _new_version(api, pub, made)
    calls = len(pub.fake.calls)
    await publish_and_run(api, pub, v2)
    sub = pub.submissions(made.listing_id)[-1]
    assert (sub["listing_version_id"], sub["operation"], sub["outcome"]) == (
        v2.version_id,
        "update",
        "partially_accepted",
    )
    ((op, fields),) = pub.fake.calls[calls:]
    assert op == "update" and fields != {} and ["content.title"] not in fields.values()
    pub.fake.set_fail_fields({})
    sku = _sku(pub, made.listing_id)
    calls = len(pub.fake.calls)
    response = await retry(api, pub, v2)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    sub = pub.submissions(made.listing_id)[-1]
    assert (sub["attempt"], sub["operation"], sub["field_paths"], sub["outcome"]) == (
        2,
        "patch",
        ["content.description"],
        "accepted",
    )
    assert pub.fake.calls[calls:] == [("update", {sku: ["content.description"]})]
