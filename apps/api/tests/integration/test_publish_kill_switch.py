"""The publish kill switch end to end (`AMAZON_PUBLISH_ENABLED=false`): the real publish job,
`AmazonAdapter` and SP-API client against the in-process fake SP-API (`amz`), plus SI11's dry run
and automatic resend on the in-process adapter (`prev`). While it is off no write request reaches
the marketplace, nothing is enqueued or counted, an audit row says why, and reads still work."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode

from .conftest import LOCAL_HOSTS
from .failure_support import AmzWorld, amz
from .preview_support import prev, preview_jobs
from .publish_support import PubWorld, approved, post_publish, publish_one, retry, run_job
from .review_support import OPERATOR, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz", "prev"]

PAUSED = "Publishing to Amazon is paused for this workspace. Nothing was sent to Amazon."


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("AMAZON_PUBLISH_ENABLED", "true" if on else "false")
    get_settings.cache_clear()


async def _run(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # RetryLater between attempts is the wrapper's signal
        assert type(exc).__name__ == "RetryLater", repr(exc)


def _publish_jobs(pw: PubWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return pw.rows(
        "SELECT id, status, error_code, payload_json->>'trigger' FROM job_runs "
        "WHERE listing_id = %s AND kind = 'listing_publish' ORDER BY queued_at, id",
        [listing_id],
    )


def _paused_audit(pw: PubWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return pw.rows(
        "SELECT actor_type, reason FROM audit_log WHERE entity_id = %s "
        "AND action = 'listing.publish_paused' ORDER BY occurred_at",
        [listing_id],
    )


# ---- the publish request: answered paused, nothing enqueued ----


async def test_publish_answers_paused_and_enqueues_nothing(
    api: httpx.AsyncClient, amz: AmzWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await approved(amz.pub)
    _switch(monkeypatch, False)
    response = await post_publish(api, amz.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    assert response.status_code == 202, response.text
    (item,) = response.json()["items"]
    assert (item["http_status"], item["job_id"], item["listing_status"]) == (501, None, "approved")
    assert item["error"]["error_code"] == ErrorCode.NOT_IMPLEMENTED.value
    assert item["error"]["retryable"] is False
    assert item["error"]["errors"][0]["message"] == PAUSED
    assert amz.pub.listing(made.listing_id)["status"] == "approved"
    assert _publish_jobs(amz.pub, made.listing_id) == []
    assert amz.pub.submissions(made.listing_id) == []
    assert amz.state.calls == [] and amz.writes() == 0  # nothing reached the marketplace
    assert _paused_audit(amz.pub, made.listing_id) == [("user", "publishing_paused")]


async def test_retry_fields_answers_paused(
    api: httpx.AsyncClient, amz: AmzWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await approved(amz.pub)
    _switch(monkeypatch, False)
    response = await retry(api, amz.pub, made)
    assert response.status_code == 501, response.text
    error = response.json()["error"]
    assert (error["type"], error["message"]) == (ErrorCode.NOT_IMPLEMENTED.value, PAUSED)
    assert _publish_jobs(amz.pub, made.listing_id) == []
    assert amz.writes() == 0


async def test_accounts_read_shows_the_switch(
    api: httpx.AsyncClient, amz: AmzWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "/api/v1/marketplaces/accounts"
    on = await api.get(url, headers=amz.world.headers(OPERATOR))
    assert on.status_code == 200, on.text
    assert [a["publishing_enabled"] for a in on.json()["data"]] == [True]
    _switch(monkeypatch, False)
    off = await api.get(url, headers=amz.world.headers(OPERATOR))
    assert [a["publishing_enabled"] for a in off.json()["data"]] == [False]


# ---- a job queued before the switch went off: fails closed, counts nothing ----


async def test_queued_publish_job_sends_nothing_and_uses_no_attempt(
    api: httpx.AsyncClient, amz: AmzWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await approved(amz.pub)
    item = await publish_one(api, amz.pub, made)
    assert item["http_status"] == 202, item
    _switch(monkeypatch, False)
    await _run(item["job_id"])
    job = amz.pub.job(item["job_id"])
    assert (job["status"], job["error_code"]) == ("failed", ErrorCode.NOT_IMPLEMENTED.value)
    assert amz.state.calls == []  # no write, no read, no token
    assert amz.pub.submissions(made.listing_id) == []  # no attempt counted
    listing = amz.pub.listing(made.listing_id)
    assert (listing["status"], listing["status_reason"]) == ("failed", "publish_failed")
    assert _paused_audit(amz.pub, made.listing_id) == [("system", "publishing_paused")]
    # once the owner says go, the same version publishes as its first attempt
    _switch(monkeypatch, True)
    again = await publish_one(api, amz.pub, made)
    assert again["http_status"] == 202, again
    await _run(again["job_id"])
    (sub,) = amz.pub.submissions(made.listing_id)
    assert sub["attempt"] == 1
    assert amz.state.submissions == 1
    assert amz.pub.listing(made.listing_id)["status"] == "published"


# ---- reads stay allowed ----


async def test_status_sync_reads_while_paused(
    api: httpx.AsyncClient, amz: AmzWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await approved(amz.pub)
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.pub.listing(made.listing_id)["status"] == "published"
    writes, reads = amz.writes(), amz.reads()
    _switch(monkeypatch, False)
    refresh = await api.post(
        f"/api/v1/listings/{made.listing_id}/refresh-status",
        headers=amz.world.headers(OPERATOR),
    )
    assert refresh.status_code == 202, refresh.text
    await _run(refresh.json()["job_id"])
    assert amz.pub.job(refresh.json()["job_id"])["status"] == "succeeded"
    assert amz.reads() > reads  # the GET went out
    assert amz.writes() == writes  # and nothing else


# ---- SI11: the dry run (a write-mode call) and the automatic resend ----


async def test_dry_run_is_not_enqueued_and_a_queued_one_makes_no_call(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued = await generated(prev.world)
    (job,) = preview_jobs(prev, queued.listing_id)
    _switch(monkeypatch, False)
    await run_job(job["id"])
    done = prev.job(job["id"])
    assert done["status"] == "succeeded"
    assert done["result_json"]["skipped"] == "publishing_paused"
    assert prev.fake.preview_calls == 0
    assert prev.listing(queued.listing_id)["remote_check_status"] == "failed"  # never pending
    fresh = await generated(prev.world)
    assert preview_jobs(prev, fresh.listing_id) == []
    assert prev.fake.preview_calls == 0


async def test_auto_resend_is_skipped_while_paused(
    api: httpx.AsyncClient, prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submission refused on a repairable value: with the switch on SI11 resends on its own
    (test_auto_resolution); with it off by the time the resolution runs, nothing is enqueued and
    no cycle is used."""
    from app.services.resolution import run

    async def enums(uow: Any, source: Any, module: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {"color": ("RED", "BLUE", "NAVY", "MAROON")}, {}

    monkeypatch.setattr(run, "_enums", enums)
    made = await approved(prev)
    prev.fake.set_fail_fields(
        {"attributes.color": ("99300", "Bad colour.", ErrorCode.VALUE_NOT_ALLOWED)}
    )
    real = prev.fake.create_listing

    async def then_pause(item: Any) -> Any:
        result = await real(item)
        _switch(monkeypatch, False)  # the owner pauses while the attempt is out
        return result

    monkeypatch.setattr(prev.fake, "create_listing", then_pause)
    item = await publish_one(api, prev, made)
    await run_job(uuid.UUID(item["job_id"]))
    jobs = _publish_jobs(prev, made.listing_id)
    assert len(jobs) == 1, jobs  # no automatic resend
    assert jobs[0][3] is None  # the user's own publish (the default trigger is not stored)
    assert prev.listing(made.listing_id)["status"] == "failed"
    assert len(prev.submissions(made.listing_id)) == 1
    resolution_jobs = prev.scalar(
        "SELECT count(*) FROM job_runs WHERE listing_id = %s AND payload_json->>'trigger' = "
        "'resolution'",
        [made.listing_id],
    )
    assert resolution_jobs == 0  # no cycle used
    assert _paused_audit(prev, made.listing_id) == [("system", "publishing_paused")]
