"""Audit JOBS-3 at job level: the real publish job, `AmazonAdapter` and SP-API client against
the in-process fake. Amazon applies the PUT, but its reply has a shape the adapter did not
expect; the job never ends `failed INTERNAL_ERROR` for a write that was applied."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .failure_support import AmzWorld, amz
from .publish_support import approved, publish_one, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz"]


async def _run(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # RetryLater between attempts is the wrapper's signal
        assert type(exc).__name__ == "RetryLater", repr(exc)


def _sku(amz: AmzWorld, listing_id: Any) -> str:
    return str(
        amz.pub.scalar(
            "SELECT p.sku FROM listings l JOIN products p ON p.id = l.product_id WHERE l.id = %s",
            [listing_id],
        )
    )


async def test_accepted_put_with_scalar_issue_names_succeeds(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)
    sku = _sku(amz, made.listing_id)
    warning = {"code": "W1", "message": "note", "severity": "WARNING", "attributeNames": 7}
    await amz.control(spapi_issues={sku: [warning]})
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    job = amz.pub.job(item["job_id"])
    assert amz.state.submissions == 1
    assert job["status"] == "succeeded", (job["status"], job["error_code"])
    assert amz.pub.listing(made.listing_id)["status"] == "published"


async def test_applied_put_with_unreadable_reply_is_read_before_retry(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)
    await amz.control(spapi_body="odd_shape")
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    job = amz.pub.job(item["job_id"])
    assert job["error_code"] != "INTERNAL_ERROR", job
    assert job["status"] != "failed", (job["status"], job["error_code"])
    (sub,) = amz.pub.submissions(made.listing_id)
    assert sub["outcome"] in (None, "pending"), sub
    assert amz.state.submissions == 1
    reads_before = amz.reads()
    # the retry reads first, finds the applied write, and never writes again
    await _run(item["job_id"])
    assert amz.reads() > reads_before
    assert amz.state.submissions == 1
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
    assert amz.pub.listing(made.listing_id)["status"] == "published"
