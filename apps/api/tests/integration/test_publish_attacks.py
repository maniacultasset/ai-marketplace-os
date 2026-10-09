"""S21 tester attacks (AC3, AC5, AC7, AC8, AC14; invariants 1, 10): image pins at publish (D9),
field retries across three attempts, concurrent publishes and overlapping batches, duplicate job
delivery, and the snapshot never carrying a signed URL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode

from .conftest import LOCAL_HOSTS
from .publish_support import (
    PubWorld,
    approved,
    post_publish,
    pub,
    publish_and_run,
    publish_one,
    retry,
    run_job,
)
from .review_support import OPERATOR, OWNER, REVIEWER, Made, generated
from .test_approve_image_gate import _image, _report, _store, _with_images

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]
RL = ("Throttled", "Request was throttled", ErrorCode.RATE_LIMITED)


# ---- image pins (D9) ----


async def _approved_with_image(pw: PubWorld, data: bytes) -> tuple[Made, uuid.UUID, str]:
    from app.adapters.storage import get_storage, images_bucket
    from app.repositories.uow import tenant_uow
    from app.services.review import ReviewItem, approve_batch

    world = pw.world
    made = await generated(world)
    image = _image(world, made)
    key = world.scalar("SELECT storage_key FROM images WHERE id = %s", [image])
    await get_storage().put(images_bucket(), key, data, "image/jpeg")
    digest = hashlib.sha256(data).hexdigest()
    await _store(world, "acme", image, _report(image))
    world.conn.execute("UPDATE images SET content_hash = %s WHERE id = %s", [digest, image])
    made = _with_images(world, made, [image])
    principal = world.principal(REVIEWER)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        outcome = await approve_batch(
            uow,
            principal,
            [ReviewItem(made.listing_id, made.version_id, "approve")],
            http_key=str(uuid.uuid4()),
        )
    assert outcome.items[0].http_status == 200, outcome.items[0]
    return made, image, key


async def _run_quiet(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # a retry signal from the wrapper is not a test failure here
        assert type(exc).__name__ == "RetryLater", exc


async def test_publish_image_pinned_sends_signed_url_snapshot_has_none(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """D5, D9, AC14: matching bytes publish; the adapter gets a signed GET URL, the stored
    snapshot names `image:<id>@<hash>` only (no URL, no signature)."""
    from app.adapters.storage import get_storage, submissions_bucket

    data = b"\xff\xd8\xff" + uuid.uuid4().bytes * 64
    made, image, _key = await _approved_with_image(pub, data)
    seen: list[Any] = []
    original = pub.fake.create_listing

    async def spy(item: Any) -> Any:
        seen.append(item)
        return await original(item)

    pub.fake.create_listing = spy
    await publish_and_run(api, pub, made)
    assert pub.listing(made.listing_id)["status"] == "published"
    urls = dict(seen[0].image_refs)
    assert set(urls) == {image} and urls[image].startswith("http"), urls
    assert "Signature" in urls[image], urls[image][:200]
    (sub,) = pub.submissions(made.listing_id)
    snapshot = (await get_storage().get(submissions_bucket(), sub["payload_storage_key"])).decode()
    digest = hashlib.sha256(data).hexdigest()
    assert f"image:{image}@{digest}" in snapshot
    assert "http" not in snapshot and "Signature" not in snapshot and "X-Amz" not in snapshot
    assert hashlib.sha256(snapshot.encode()).digest() == bytes(sub["payload_sha256"])


async def test_publish_image_hash_mismatch(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """AC8, D9: the bytes changed after approval → nothing sent, no row, approval voided, the
    job failed IMAGE_NON_COMPLIANT on images[0]."""
    from app.adapters.storage import get_storage, images_bucket

    made, _image_id, key = await _approved_with_image(pub, b"\xff\xd8\xff" + b"a" * 300)
    item = await publish_one(api, pub, made)
    await get_storage().put(images_bucket(), key, b"\xff\xd8\xff" + b"b" * 300, "image/jpeg")
    await _run_quiet(item["job_id"])
    job = pub.job(item["job_id"])
    assert (job["status"], job["error_code"]) == ("failed", "IMAGE_NON_COMPLIANT")
    assert "images[0]" in json.dumps(job["error_report_json"], default=str)
    assert pub.fake.calls == [] and pub.submissions(made.listing_id) == []
    row = pub.listing(made.listing_id)
    assert row["status"] in ("needs_review", "blocked") and row["approved_version_id"] is None


async def test_publish_image_unchecked_refused(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """D9: a `content_hash` that went NULL (never checked) fails closed; nothing is sent."""
    made, image, _key = await _approved_with_image(pub, b"\xff\xd8\xff" + b"c" * 300)
    item = await publish_one(api, pub, made)
    pub.conn.execute("UPDATE images SET content_hash = NULL WHERE id = %s", [image])
    await _run_quiet(item["job_id"])
    job = pub.job(item["job_id"])
    assert (job["status"], job["error_code"]) == ("failed", "IMAGE_NON_COMPLIANT")
    assert pub.fake.calls == [] and pub.submissions(made.listing_id) == []


async def test_publish_image_object_missing_refused(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """D9: the object is gone from storage → refused, nothing sent (no 500, no retry loop)."""
    from app.adapters.storage import get_storage, images_bucket

    made, _image_id, key = await _approved_with_image(pub, b"\xff\xd8\xff" + b"d" * 300)
    item = await publish_one(api, pub, made)
    await get_storage().delete(images_bucket(), key)
    await _run_quiet(item["job_id"])
    job = pub.job(item["job_id"])
    assert (job["status"], job["error_code"]) == ("failed", "IMAGE_NON_COMPLIANT")
    assert pub.fake.calls == []


# ---- invariant 10 across three attempts ----


def _failed_fields(pw: PubWorld, submission_id: Any) -> list[str]:
    return [
        f
        for (f,) in pw.rows(
            "SELECT field FROM publish_field_results WHERE submission_id = %s "
            "AND status = 'failed' ORDER BY field",
            [submission_id],
        )
    ]


async def test_retry_twice_sends_only_still_failed_then_cap(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """Attempt 1 fails 2 fields; attempt 2 (retry) fixes one; attempt 3 resends only the one
    still failing, and a 3rd non-success blocks the listing; a 4th is refused with no job."""
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL, "content.description": RL})
    await publish_and_run(api, pub, made)
    pub.fake.set_fail_fields({"content.description": RL})
    second = await retry(api, pub, made)
    assert second.status_code == 202, second.text
    await run_job(uuid.UUID(second.json()["job_id"]))
    subs = pub.submissions(made.listing_id)
    assert [(s["attempt"], s["operation"], s["outcome"]) for s in subs] == [
        (1, "create", "partially_accepted"),
        (2, "patch", "partially_accepted"),
    ]
    assert sorted(subs[1]["field_paths"]) == ["content.description", "content.title"]
    assert _failed_fields(pub, subs[1]["id"]) == ["content.description"]
    assert pub.listing(made.listing_id)["status_reason"] == "publish_partial"
    third = await retry(api, pub, made)
    assert third.status_code == 202, third.text
    await run_job(uuid.UUID(third.json()["job_id"]))
    subs = pub.submissions(made.listing_id)
    assert (subs[2]["attempt"], subs[2]["operation"], subs[2]["field_paths"]) == (
        3,
        "patch",
        ["content.description"],
    )
    op, sent = pub.fake.calls[-1]
    assert op == "update" and {tuple(v) for v in sent.values()} == {("content.description",)}
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "publish_attempts_exhausted")
    jobs = pub.scalar("SELECT count(*) FROM job_runs WHERE listing_id = %s", [made.listing_id])
    fourth = await retry(api, pub, made)
    assert fourth.status_code == 409, fourth.text
    assert fourth.json()["error"]["type"] == "SUBMISSION_ATTEMPTS_EXHAUSTED"
    again = await publish_one(api, pub, made)
    assert (again["http_status"], again["error"]["error_code"]) == (
        409,
        "SUBMISSION_ATTEMPTS_EXHAUSTED",
    )
    assert (
        pub.scalar("SELECT count(*) FROM job_runs WHERE listing_id = %s", [made.listing_id]) == jobs
    )


# ---- races (AC7) ----


async def test_concurrent_publish_two_keys_one_wins(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    body = [{"listing_id": str(made.listing_id)}]
    first, second = await asyncio.gather(
        post_publish(api, pub, OPERATOR, body), post_publish(api, pub, OWNER, body)
    )
    assert first.status_code == 202 and second.status_code == 202, (first.text, second.text)
    items = [first.json()["items"][0], second.json()["items"][0]]
    assert sorted(i["http_status"] for i in items) == [202, 409]
    loser = next(i for i in items if i["http_status"] == 409)
    assert loser["error"]["error_code"] == "LISTING_BUSY"
    assert (
        pub.scalar(
            "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
            [made.listing_id],
        )
        == 1
    )
    assert (
        pub.scalar(
            "SELECT count(*) FROM batches WHERE kind = 'publish' AND id = ANY(%s)",
            [[uuid.UUID(first.json()["batch_id"]), uuid.UUID(second.json()["batch_id"])]],
        )
        == 2
    )


async def test_overlapping_batches_reverse_order_no_deadlock(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """Two batches naming the same two listings in opposite orders: locks in id order, so both
    requests answer (no 500 deadlock) and each listing gets exactly one job."""
    a = await approved(pub)
    b = await approved(pub)
    ab = [{"listing_id": str(a.listing_id)}, {"listing_id": str(b.listing_id)}]
    first, second = await asyncio.wait_for(
        asyncio.gather(
            post_publish(api, pub, OPERATOR, ab), post_publish(api, pub, OWNER, ab[::-1])
        ),
        timeout=60,
    )
    assert (first.status_code, second.status_code) == (202, 202), (first.text, second.text)
    statuses = [i["http_status"] for r in (first, second) for i in r.json()["items"]]
    assert sorted(statuses) == [202, 202, 409, 409]
    for made in (a, b):
        assert (
            pub.scalar(
                "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
                [made.listing_id],
            )
            == 1
        )


# ---- duplicate delivery ----


async def test_duplicate_job_delivery_sends_once(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """A second delivery of a finished publish job makes no adapter call and no second row."""
    made = await approved(pub)
    item = await publish_and_run(api, pub, made)
    calls = len(pub.fake.calls)
    await _run_quiet(item["job_id"])
    assert len(pub.fake.calls) == calls
    assert [s["outcome"] for s in pub.submissions(made.listing_id)] == ["accepted"]
    assert pub.job(item["job_id"])["status"] == "succeeded"


async def test_publish_published_listing_409_no_job(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """A `published` listing has no publish edge: item 409, no job, the adapter untouched."""
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    calls = len(pub.fake.calls)
    again = await publish_one(api, pub, made)
    assert (again["http_status"], again["error"]["error_code"]) == (
        409,
        "INVALID_STATUS_TRANSITION",
    )
    assert len(pub.fake.calls) == calls
    assert (
        pub.scalar(
            "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
            [made.listing_id],
        )
        == 1
    )


# ---- a new version after a partial or an exhausted version (AC5) ----


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


async def test_new_version_after_partial_attempt_1(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """A partial applied part of the listing at the marketplace; an edited, re-approved version
    starts at attempt 1 (the `create` vs `update` choice is a tester finding: see
    docs/plans/s21/repro/test_repro_partial_then_new_version.py)."""
    from .review_support import edited

    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    assert pub.listing(made.listing_id)["status_reason"] == "publish_partial"
    pub.fake.set_fail_fields({})
    v2 = await edited(api, pub.world, made, email=OWNER)
    await _approve(pub, v2)
    await publish_and_run(api, pub, v2)
    subs = pub.submissions(made.listing_id)
    assert [(s["listing_version_id"], s["attempt"], s["outcome"]) for s in subs] == [
        (made.version_id, 1, "partially_accepted"),
        (v2.version_id, 1, "accepted"),
    ]


async def test_new_version_after_cap_starts_at_attempt_1(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """AC5: blocked/publish_attempts_exhausted → edit → approve → publish is attempt 1."""
    from app.adapters.base import RateLimited
    from mkos_schemas.errors import ErrorReport

    from .review_support import edited

    made = await approved(pub)
    for _ in range(3):
        pub.fake.raise_next.append(RateLimited(ErrorReport(status="failed")))
    item = await publish_one(api, pub, made)
    for _ in range(3):
        await _run_quiet(item["job_id"])
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "publish_attempts_exhausted")
    v2 = await edited(api, pub.world, made, email=OWNER)
    await _approve(pub, v2)
    pub.fake.raise_next.clear()
    nxt = await publish_one(api, pub, v2)
    assert nxt["http_status"] == 202, nxt
    await run_job(uuid.UUID(nxt["job_id"]))
    subs = [s for s in pub.submissions(made.listing_id) if s["listing_version_id"] == v2.version_id]
    assert [(s["attempt"], s["outcome"]) for s in subs] == [(1, "accepted")]
