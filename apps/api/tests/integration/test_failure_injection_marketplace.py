"""S26 T12 (AC 5, DW1): marketplace faults injected into the publish job.

The job runs through the real `AmazonAdapter` and SP-API client against the in-process fake
(`failure_support.amz`). Each test asserts the final listing and job state, exactly one
`ErrorReport` where a failure remains, and the fake's applied-write count (no duplicate write).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .failure_support import AmzWorld, amz
from .publish_support import approved, publish_one, retry, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz"]


async def _run(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # RetryLater between attempts is the wrapper's signal
        assert type(exc).__name__ == "RetryLater", repr(exc)


async def _publish(api: httpx.AsyncClient, aw: AmzWorld) -> tuple[Any, dict[str, Any]]:
    made = await approved(aw.pub)
    item = await publish_one(api, aw.pub, made)
    assert item["http_status"] == 202, item
    await _run(item["job_id"])
    return made, aw.pub.job(item["job_id"])


def assert_writes(aw: AmzWorld, want: int) -> None:
    """The applied-write check every fault test ends with: the fake applied exactly `want`
    PUT/PATCH writes (a T16 prove leg turns this check off and the drop test goes red)."""
    assert aw.state.submissions == want, aw.state.calls


def one_report(row: dict[str, Any]) -> dict[str, Any]:
    """A failed attempt's ErrorReport: one object in the one shape (invariant 8)."""
    from mkos_schemas.errors import ErrorReport

    report = row["error_report_json"]
    assert isinstance(report, dict), row
    ErrorReport.model_validate(report)
    return report


async def test_failure_injection_429_backoff_then_success(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    await amz.control(spapi_429=2)
    made, job = await _publish(api, amz)
    assert job["status"] == "succeeded", job
    assert amz.pub.listing(made.listing_id)["status"] == "published"
    assert len(amz.sleeps) == 2 and all(0 < s <= 60 for s in amz.sleeps), amz.sleeps
    assert_writes(amz, 1)


async def test_failure_injection_token_expired_mid_publish_one_refresh(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    await amz.control(spapi_auth={"mode": "expired", "times": 1})
    _, job = await _publish(api, amz)
    assert job["status"] == "succeeded", job
    assert amz.tokens.refreshes == 1
    assert_writes(amz, 1)


async def test_failure_injection_invalid_grant_auth_expired_reconnect_task(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    amz.tokens.refuse_refresh = True
    await amz.control(spapi_auth={"mode": "expired", "times": 5})
    made, _ = await _publish(api, amz)
    row = amz.pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "marketplace_action_required")
    (sub,) = amz.pub.submissions(made.listing_id)
    assert (sub["outcome"], sub["error_code"]) == ("error", "AUTH_EXPIRED")
    assert one_report(sub)["error_code"] == "AUTH_EXPIRED"
    tasks = amz.pub.rows(
        "SELECT t.kind FROM listing_issues i JOIN human_tasks t ON t.id = i.human_task_id "
        "WHERE i.listing_id = %s",
        [made.listing_id],
    )
    assert tasks == [("reconnect_account",)]
    assert_writes(amz, 0)


async def test_failure_injection_partial_two_of_five_fields(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    from tests.adapters.amazon_support import attribute_names

    rejected = [
        n for path in ("content.title", "content.description") for n in attribute_names(path)
    ]
    for name in rejected:
        amz.state.spapi_reject[name] = ("90220", "The value is not allowed.")
    made, job = await _publish(api, amz)
    assert job["status"] == "partial", job
    (sub,) = amz.pub.submissions(made.listing_id)
    assert sub["outcome"] == "partially_accepted"
    failed = amz.pub.rows(
        "SELECT field FROM publish_field_results WHERE submission_id = %s AND status = 'failed' "
        "ORDER BY field",
        [sub["id"]],
    )
    assert [f for (f,) in failed] == ["content.description", "content.title"]
    total = amz.pub.scalar(
        "SELECT count(*) FROM publish_field_results WHERE submission_id = %s", [sub["id"]]
    )
    assert total >= 3
    one_report(sub)
    assert_writes(amz, 1)


async def test_failure_injection_drop_after_write_pending_read_before_retry(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)
    sku = amz.pub.scalar(
        "SELECT p.sku FROM listings l JOIN products p ON p.id = l.product_id WHERE l.id = %s",
        [made.listing_id],
    )
    await amz.control(spapi_drop_after_write=[sku])
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    (sub,) = amz.pub.submissions(made.listing_id)
    assert sub["outcome"] in (None, "pending") or sub["responded_at"] is None, sub
    assert_writes(amz, 1)
    reads_before = amz.reads()
    # the next attempt (the job's retry) reads first, finds the write, and never writes again
    await _run(item["job_id"])
    assert amz.reads() > reads_before
    assert_writes(amz, 1)
    assert amz.pub.job(item["job_id"])["status"] in ("succeeded", "running", "queued")


async def test_failure_injection_5xx_after_write_unconfirmed(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)
    sku = amz.pub.scalar(
        "SELECT p.sku FROM listings l JOIN products p ON p.id = l.product_id WHERE l.id = %s",
        [made.listing_id],
    )
    await amz.control(spapi_after_write={sku: 500})
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    (sub,) = amz.pub.submissions(made.listing_id)
    assert sub["outcome"] in (None, "pending"), sub
    assert_writes(amz, 1)


async def test_failure_injection_retry_resends_only_failed_fields(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    from tests.adapters.amazon_support import attribute_names

    from .failure_support import RETRYABLE_CODE

    names = attribute_names("content.title")
    for name in names:
        amz.state.spapi_reject[name] = (RETRYABLE_CODE, "Throttled for this attribute.")
    made, job = await _publish(api, amz)
    assert job["status"] == "partial", job
    amz.state.spapi_reject.clear()
    response = await retry(api, amz.pub, made)
    assert response.status_code == 202, response.text
    await _run(response.json()["job_id"])
    last = amz.state.calls[-1]
    assert last["method"] == "PATCH", last
    subs = amz.pub.submissions(made.listing_id)
    assert subs[-1]["field_paths"] == ["content.title"]
    assert subs[-1]["retry_justification"] == "retryable_condition"
    sent = amz.pub.rows(
        "SELECT field FROM publish_field_results WHERE submission_id = %s", [subs[-1]["id"]]
    )
    assert {f for (f,) in sent} == {"content.title"}
    assert_writes(amz, 2)
