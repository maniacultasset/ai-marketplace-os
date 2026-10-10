"""S21 T9-T16 (AC1-AC5; DW1-DW4): publish an approved listing through the FakeAdapter, the
job's ledger writes, partial results, field retries and the cap."""

from __future__ import annotations

import uuid

import httpx
import pytest
from mkos_schemas.codes import ErrorCode

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, pub, publish_and_run, publish_one, retry, run_job
from .review_support import OPERATOR

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]
RL = ("Throttled", "Request was throttled", ErrorCode.RATE_LIMITED)


async def test_publish_happy(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    item = await publish_one(api, pub, made)
    assert (item["http_status"], item["listing_status"]) == (202, "publishing")
    job = pub.job(item["job_id"])
    assert (job["kind"], job["queue"], job["created_by"]) == (
        "listing_publish",
        "publish",
        pub.world.user(OPERATOR),
    )
    assert job["batch_id"] is not None
    await run_job(uuid.UUID(item["job_id"]))
    row = pub.listing(made.listing_id)
    assert (row["status"], row["marketplace_state"], row["published_version_id"]) == (
        "published",
        "processing",
        made.version_id,
    )
    assert row["first_published_at"] is not None and row["next_sync_at"] is not None
    assert row["live_url"] is None and row["sync_poll_count"] == 0
    (sub,) = pub.submissions(made.listing_id)
    assert (sub["attempt"], sub["operation"], sub["outcome"]) == (1, "create", "accepted")
    assert sub["payload_storage_key"].startswith(f"{pub.world.tenant('acme')}/submissions/")
    fields = pub.rows(
        "SELECT field, status FROM publish_field_results WHERE submission_id = %s", [sub["id"]]
    )
    assert fields and all(status == "succeeded" for _, status in fields)
    assert pub.job(item["job_id"])["status"] == "succeeded"


async def test_publish_partial_retry(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """DW2: 2 of the fields fail with a retryable code → partial; the retry sends only those."""
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL, "content.description": RL})
    item = await publish_and_run(api, pub, made)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "publish_partial")
    assert pub.job(item["job_id"])["status"] == "partial"
    (sub,) = pub.submissions(made.listing_id)
    assert sub["outcome"] == "partially_accepted"
    failed = pub.rows(
        "SELECT field FROM publish_field_results WHERE submission_id = %s AND status = 'failed' "
        "ORDER BY field",
        [sub["id"]],
    )
    assert [f for (f,) in failed] == ["content.description", "content.title"]
    issues = pub.scalar(
        "SELECT count(*) FROM listing_issues WHERE listing_id = %s AND status = 'open' "
        "AND source = 'amazon_adapter'",
        [made.listing_id],
    )
    assert issues == 2
    pub.fake.set_fail_fields({})
    response = await retry(api, pub, made)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert pub.job(job_id)["parent_job_id"] == uuid.UUID(item["job_id"])
    await run_job(uuid.UUID(job_id))
    subs = pub.submissions(made.listing_id)
    assert [(s["attempt"], s["operation"], s["outcome"]) for s in subs] == [
        (1, "create", "partially_accepted"),
        (2, "patch", "accepted"),
    ]
    assert sorted(subs[1]["field_paths"]) == ["content.description", "content.title"]
    assert subs[1]["retry_justification"] == "retryable_condition"
    op, sent = pub.fake.calls[-1]
    assert op == "update" and {tuple(v) for v in sent.values()} == {
        ("content.description", "content.title")
    }
    assert pub.listing(made.listing_id)["status"] == "published"
    assert (
        pub.scalar(
            "SELECT count(*) FROM listing_issues WHERE listing_id = %s AND status = 'open' "
            "AND source = 'amazon_adapter'",
            [made.listing_id],
        )
        == 0
    )


async def test_retry_fields_user_action_failure_409_no_job(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": ("BadTitle", "bad", ErrorCode.VALUE_NOT_ALLOWED)})
    await publish_and_run(api, pub, made)
    assert pub.listing(made.listing_id)["status_reason"] == "publish_partial"
    jobs = pub.scalar("SELECT count(*) FROM job_runs WHERE listing_id = %s", [made.listing_id])
    response = await retry(api, pub, made)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "DUPLICATE_SUBMISSION_BLOCKED"
    assert (
        pub.scalar("SELECT count(*) FROM job_runs WHERE listing_id = %s", [made.listing_id]) == jobs
    )


async def test_submission_cap(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """DW4: three rate-limited attempts block the listing; a 4th is refused, no job."""
    from app.adapters.base import RateLimited
    from mkos_schemas.errors import ErrorReport

    made = await approved(pub)
    pub.fake.raise_next = [RateLimited(ErrorReport(status="failed")) for _ in range(3)]
    item = await publish_one(api, pub, made)
    for _ in range(3):
        try:
            await run_job(uuid.UUID(item["job_id"]))
        except Exception as exc:  # RetryLater between attempts
            assert type(exc).__name__ == "RetryLater"
    subs = pub.submissions(made.listing_id)
    assert [(s["attempt"], s["outcome"], s["error_code"]) for s in subs] == [
        (1, "error", "RATE_LIMITED"),
        (2, "error", "RATE_LIMITED"),
        (3, "error", "RATE_LIMITED"),
    ]
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "publish_attempts_exhausted")
    again = await publish_one(api, pub, made)
    assert again["http_status"] == 409
    assert again["error"]["error_code"] == "SUBMISSION_ATTEMPTS_EXHAUSTED"
    response = await retry(api, pub, made)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "SUBMISSION_ATTEMPTS_EXHAUSTED"
