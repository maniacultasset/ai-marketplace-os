"""DW2 T10 (AC8, AC9, AC13): the publish job checks a first publish (`create_listing`) against
the current stored Amazon definition, read at job time: the real publish job, `AmazonAdapter` and
SP-API client against the in-process fake marketplace. An update (PATCH) is never blocked by it
(D5, AC9)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from app.adapters.amazon import schema_check

from .conftest import LOCAL_HOSTS
from .failure_support import AmzWorld, amz
from .ptd_support import promote, store
from .publish_support import approved, publish_one, retry, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz"]

PERMISSIVE: dict[str, Any] = {"type": "object", "title": "permissive DW2 test schema"}
STRICT: dict[str, Any] = {
    "type": "object",
    "title": "strict DW2 test schema",
    "required": ["brand"],
    "properties": {"brand": {"title": "Brand", "type": "array"}},
}


@pytest.fixture(autouse=True)
def _cold_cache() -> None:
    schema_check.clear_cache()


async def _run(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # RetryLater between attempts is the wrapper's signal
        assert type(exc).__name__ == "RetryLater", repr(exc)


def _field_errors(amz: AmzWorld, listing_id: Any) -> list[dict[str, Any]]:
    rows = amz.pub.rows(
        "SELECT error_json FROM publish_field_results WHERE listing_id = %s AND error_json "
        "IS NOT NULL",
        [listing_id],
    )
    found: list[dict[str, Any]] = []
    for (error,) in rows:
        found += error if isinstance(error, list) else [error]
    return found


def _ptd_errors(amz: AmzWorld, listing_id: Any, job: dict[str, Any]) -> list[str]:
    text = json.dumps(_field_errors(amz, listing_id)) + json.dumps(
        job.get("error_report_json") or job.get("error_json") or {}, default=str
    )
    return [part for part in text.split('"') if part.startswith("ptd:")]


async def test_invalid_listing_is_never_sent(api: httpx.AsyncClient, amz: AmzWorld) -> None:
    made = await approved(amz.pub)
    # SI9 B4: a definition stored before the generation blocks it before approval; this one
    # arrives after the approval, so the publish job's own check is what refuses it
    store(amz.pub.conn, STRICT)
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.writes() == 0
    job = amz.pub.job(item["job_id"])
    assert job["status"] != "succeeded", job
    assert amz.pub.listing(made.listing_id)["status"] != "published"
    assert "ptd:required:UFAKE0001" in _ptd_errors(amz, made.listing_id, job)


async def test_valid_listing_publishes_as_today(api: httpx.AsyncClient, amz: AmzWorld) -> None:
    store(amz.pub.conn, PERMISSIVE)
    made = await approved(amz.pub)
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.writes() >= 1
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
    assert amz.pub.listing(made.listing_id)["status"] == "published"


async def test_promotion_before_the_job_uses_the_new_version(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    store(amz.pub.conn, PERMISSIVE, version="UFAKE0001")
    newer = store(amz.pub.conn, STRICT, version="UFAKE0002", current=False)
    made = await approved(amz.pub)
    item = await publish_one(api, amz.pub, made)
    promote(amz.pub.conn, newer)
    await _run(item["job_id"])
    assert amz.writes() == 0
    job = amz.pub.job(item["job_id"])
    codes = _ptd_errors(amz, made.listing_id, job)
    assert "ptd:required:UFAKE0002" in codes and "ptd:required:UFAKE0001" not in codes


TITLE_MAX_1: dict[str, Any] = {
    "type": "object",
    "title": "rejects every title",
    "properties": {
        "item_name": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"value": {"type": "string", "maxLength": 1}},
            },
        }
    },
}


async def test_update_patch_is_not_blocked_by_a_schema_error(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    """Review round 1: a schema error on content.title does not withhold it from the retry PATCH;
    Amazon's reply decides the result."""
    from tests.adapters.amazon_support import attribute_names

    from .failure_support import RETRYABLE_CODE

    store(amz.pub.conn, PERMISSIVE)
    for name in attribute_names("content.title"):
        amz.state.spapi_reject[name] = (RETRYABLE_CODE, "Throttled for this attribute.")
    made = await approved(amz.pub)
    first = await publish_one(api, amz.pub, made)
    await _run(first["job_id"])
    assert amz.pub.job(first["job_id"])["status"] == "partial"
    amz.state.spapi_reject.clear()
    store(amz.pub.conn, TITLE_MAX_1, version="UFAKE0002")
    response = await retry(api, amz.pub, made)
    assert response.status_code == 202, response.text
    await _run(response.json()["job_id"])
    last = amz.state.calls[-1]
    assert last["method"] == "PATCH", last
    sub = amz.pub.submissions(made.listing_id)[-1]
    assert sub["field_paths"] == ["content.title"]
    sent = amz.pub.rows(
        "SELECT field, status FROM publish_field_results WHERE submission_id = %s", [sub["id"]]
    )
    assert set(sent) == {("content.title", "succeeded")}
    assert amz.pub.job(response.json()["job_id"])["status"] == "succeeded"
