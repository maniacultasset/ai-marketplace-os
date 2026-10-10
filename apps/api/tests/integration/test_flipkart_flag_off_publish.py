"""S22 T20, T21 (AC17, AC19; C3, C12; D5, D10): with `FLIPKART_ENABLED=false` a publish batch
answers an item 501 for a Flipkart listing (Amazon items are accepted as usual), retry-fields
answers the whole-request 501 envelope, a Flipkart job claimed while off sends nothing, and the
status sync skips Flipkart. The flag check comes after the tenant-scoped lookup (a foreign id is
404, never 501) and before every other item check (status, account)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .flipkart_publish_support import FkWorld, fkp, flag, pub
from .publish_support import approved, post_publish, retry, run_job
from .review_support import OPERATOR, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["fkp", "pub"]
BHARAT = "operator@bharat.test"


def _item_501(item: dict[str, Any], listing_id: uuid.UUID) -> None:
    assert item["http_status"] == 501, item
    assert item["job_id"] is None, item
    error = item["error"]
    assert error["error_code"] == "NOT_IMPLEMENTED", error
    assert error["listing_id"] == str(listing_id), error
    (one,) = error["errors"]
    assert (one["message"], one["source"]) == ("Flipkart is not enabled", "system"), one


def _envelope_501(response: httpx.Response) -> None:
    assert response.status_code == 501, response.text
    error = response.json()["error"]
    assert error["type"] == "NOT_IMPLEMENTED", error
    assert error["message"] == "Flipkart is not enabled", error


def _untouched(fk: FkWorld, listing_id: uuid.UUID, status: str) -> None:
    assert fk.jobs(listing_id) == 0
    assert fk.pub.submissions(listing_id) == []
    row = fk.pub.listing(listing_id)
    assert (row["status"], row["marketplace_account_id"]) == (status, None), row
    assert fk.calls() == [] and fk.opened == []


def _keys(fk: FkWorld, key: str) -> int:
    return int(fk.pub.scalar("SELECT count(*) FROM idempotency_keys WHERE key = %s", [key]))


def _shape(item: dict[str, Any]) -> str:
    """An item without its own listing id and request id, for byte-equal comparison."""
    error = dict(item["error"])
    error.pop("request_id", None)
    error.pop("listing_id", None)
    return json.dumps({**item, "listing_id": None, "error": error}, sort_keys=True)


async def _fk(fk: FkWorld, **kw: Any) -> Any:
    return await approved(fk.pub, marketplace="flipkart", **kw)


@pytest.mark.endpoint("listings_publish", case="success", variant="flipkart_off")
async def test_flipkart_flag_off_publish(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _fk(fkp)
    flag(monkeypatch, False)
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    assert response.status_code == 202, response.text
    (item,) = response.json()["items"]
    _item_501(item, made.listing_id)
    assert item["listing_status"] == "approved"
    _untouched(fkp, made.listing_id, "approved")


async def test_flipkart_flag_off_retry_fields(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _fk(fkp)
    flag(monkeypatch, False)
    key = str(uuid.uuid4())
    response = await retry(api, fkp.pub, made, key=key)
    _envelope_501(response)
    assert _keys(fkp, key) == 0  # nothing stored: the next try with this key runs afresh
    _untouched(fkp, made.listing_id, "approved")


async def test_flipkart_flag_off_mixed_batch_amazon_accepted_flipkart_item_501(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    amazon = await approved(fkp.pub)
    flipkart = await _fk(fkp)
    flag(monkeypatch, False)
    body = [{"listing_id": str(amazon.listing_id)}, {"listing_id": str(flipkart.listing_id)}]
    response = await post_publish(api, fkp.pub, OPERATOR, body)
    assert response.status_code == 202, response.text
    first, second = response.json()["items"]
    assert (first["http_status"], first["listing_status"]) == (202, "publishing")
    assert first["job_id"] is not None
    assert fkp.pub.listing(amazon.listing_id)["status"] == "publishing"
    _item_501(second, flipkart.listing_id)
    _untouched(fkp, flipkart.listing_id, "approved")


async def test_flipkart_flag_off_mixed_batch_replay_same_body(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    amazon = await approved(fkp.pub)
    flipkart = await _fk(fkp)
    flag(monkeypatch, False)
    key = str(uuid.uuid4())
    body = [{"listing_id": str(amazon.listing_id)}, {"listing_id": str(flipkart.listing_id)}]
    first = await post_publish(api, fkp.pub, OPERATOR, body, key=key)
    again = await post_publish(api, fkp.pub, OPERATOR, body, key=key)
    assert first.status_code == again.status_code == 202
    assert first.content == again.content
    assert fkp.jobs(amazon.listing_id) == 1 and fkp.jobs(flipkart.listing_id) == 0


async def test_flipkart_flag_off_after_flag_on_same_key_replays_501(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _fk(fkp)
    flag(monkeypatch, False)
    key = str(uuid.uuid4())
    body = [{"listing_id": str(made.listing_id)}]
    off = await post_publish(api, fkp.pub, OPERATOR, body, key=key)
    flag(monkeypatch, True)
    replay = await post_publish(api, fkp.pub, OPERATOR, body, key=key)
    assert off.content == replay.content  # the stored 202 batch, item 501 and all
    (item,) = replay.json()["items"]
    _item_501(item, made.listing_id)
    assert fkp.jobs(made.listing_id) == 0


async def test_flipkart_flag_off_after_flag_on_new_key_accepted(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _fk(fkp)
    flag(monkeypatch, False)
    body = [{"listing_id": str(made.listing_id)}]
    await post_publish(api, fkp.pub, OPERATOR, body)
    flag(monkeypatch, True)
    response = await post_publish(api, fkp.pub, OPERATOR, body)
    (item,) = response.json()["items"]
    assert (item["http_status"], item["listing_status"]) == (202, "publishing"), item
    assert fkp.jobs(made.listing_id) == 1


async def test_flipkart_flag_off_flag_before_no_account(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No active Flipkart account and the flag off: the flag answers (501), not 409 AUTH_EXPIRED."""
    made = await _fk(fkp)
    fkp.pub.conn.execute(
        "UPDATE marketplace_accounts SET status = 'needs_reconnect', needs_reconnect_since = now() "
        "WHERE id = %s",
        [fkp.accounts["acme"]],
    )
    flag(monkeypatch, False)
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    _item_501(item, made.listing_id)


async def test_flipkart_flag_off_flag_before_status(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Flipkart listing never approved, flag off: 501, not S21's not-approved 409."""
    made = await generated(fkp.world, marketplace="flipkart")
    status = fkp.pub.listing(made.listing_id)["status"]
    assert status != "approved"
    flag(monkeypatch, False)
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    _item_501(item, made.listing_id)
    assert item["listing_status"] == status
    _untouched(fkp, made.listing_id, status)


async def test_flipkart_flag_off_publish_cross_tenant_404_not_501(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _fk(fkp)
    flag(monkeypatch, False)
    foreign = await post_publish(api, fkp.pub, BHARAT, [{"listing_id": str(made.listing_id)}])
    random = await post_publish(api, fkp.pub, BHARAT, [{"listing_id": str(uuid.uuid4())}])
    (theirs,) = foreign.json()["items"]
    (unknown,) = random.json()["items"]
    assert (theirs["http_status"], theirs["error"]["error_code"]) == (404, "NOT_FOUND")
    assert _shape(theirs) == _shape(unknown)
    _untouched(fkp, made.listing_id, "approved")


async def test_flipkart_flag_off_retry_fields_cross_tenant_404_not_501(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _fk(fkp)
    flag(monkeypatch, False)
    foreign = await retry(api, fkp.pub, made, BHARAT)
    random = await api.post(
        f"/api/v1/listings/{uuid.uuid4()}/publish/retry-fields",
        headers=fkp.world.headers(BHARAT),
    )
    assert foreign.status_code == random.status_code == 404
    mine, other = foreign.json()["error"], random.json()["error"]
    for error in (mine, other):
        for per_request in ("request_id", "correlation_id"):
            error.pop(per_request, None)
            (error.get("error_report") or {}).pop(per_request, None)
    assert mine == other


async def test_flipkart_flag_off_mixed_batch_foreign_flipkart_id_404(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme_fk = await _fk(fkp)
    bharat_amazon = await approved(fkp.pub, maker="operator@bharat.test")
    flag(monkeypatch, False)
    body = [{"listing_id": str(bharat_amazon.listing_id)}, {"listing_id": str(acme_fk.listing_id)}]
    response = await post_publish(api, fkp.pub, BHARAT, body)
    first, second = response.json()["items"]
    assert first["http_status"] == 202 and first["job_id"] is not None
    assert (second["http_status"], second["error"]["error_code"]) == (404, "NOT_FOUND")
    _untouched(fkp, acme_fk.listing_id, "approved")


# ---- T21: a job claimed while off; the status sync ----


async def test_flipkart_job_claimed_flag_off(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enqueued with the flag on, claimed with it off: nothing is written ahead or sent, the job
    fails with the 501 report and the listing leaves `publishing` for `failed/publish_failed`."""
    made = await _fk(fkp)
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    assert item["http_status"] == 202, item
    flag(monkeypatch, False)
    await run_job(uuid.UUID(item["job_id"]))
    assert fkp.pub.submissions(made.listing_id) == []
    assert fkp.calls() == [] and fkp.opened == []
    row = fkp.pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"], row["last_event"]) == (
        "failed",
        "publish_failed",
        "submission_failed",
    )
    job = fkp.pub.job(item["job_id"])
    assert job["status"] == "failed"
    report = job["error_report_json"]
    assert report["error_code"] == "NOT_IMPLEMENTED", report
    assert report["errors"][0]["message"] == "Flipkart is not enabled"


async def test_flipkart_job_claimed_flag_off_then_on_publishes_attempt_1(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _fk(fkp)
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    flag(monkeypatch, False)
    await run_job(uuid.UUID(item["job_id"]))
    flag(monkeypatch, True)
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (again,) = response.json()["items"]
    assert again["http_status"] == 202, again
    assert fkp.pub.listing(made.listing_id)["last_event"] == "retry_publish"
    await run_job(uuid.UUID(again["job_id"]))
    (sub,) = fkp.pub.submissions(made.listing_id)
    assert (sub["attempt"], sub["operation"], sub["outcome"]) == (1, "create", "accepted")
    assert fkp.pub.listing(made.listing_id)["status"] == "published"
    assert len(fkp.calls()) == 1


async def test_flipkart_sync_skipped(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published Flipkart listing: with the flag off the sync job reads nothing and changes
    nothing but the sync slot the refresh cleared, which it puts back (S22b F5), and the
    scheduler does not pick it up (it resumes once the flag is on)."""
    from app.workers.periodic import schedule_status_sync

    made = await _fk(fkp)
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    await run_job(uuid.UUID(item["job_id"]))
    before = fkp.pub.listing(made.listing_id)
    assert before["status"] == "published"
    calls = len(fkp.calls())
    fkp.pub.conn.execute(
        "UPDATE listings SET next_sync_at = now() - interval '1 minute' WHERE id = %s",
        [made.listing_id],
    )
    flag(monkeypatch, False)
    await schedule_status_sync()
    assert fkp.jobs(made.listing_id, "listing_status_sync") == 0
    assert fkp.pub.listing(made.listing_id)["next_sync_at"] is not None  # kept for later
    flag(monkeypatch, True)
    refresh = await api.post(
        f"/api/v1/listings/{made.listing_id}/refresh-status",
        headers=fkp.world.headers(OPERATOR),
    )
    assert refresh.status_code == 202, refresh.text
    sync = uuid.UUID(refresh.json()["job_id"])
    before = fkp.pub.listing(made.listing_id)
    flag(monkeypatch, False)
    await run_job(sync)
    after = fkp.pub.listing(made.listing_id)
    assert fkp.pub.job(sync)["status"] == "succeeded"
    assert len(fkp.calls()) == calls and len(fkp.opened) == 1
    for column in ("status", "marketplace_state", "sync_poll_count", "live_url"):
        assert after[column] == before[column], column
    assert before["next_sync_at"] is None  # the refresh cleared it
    assert after["next_sync_at"] is not None  # the skip put the slot back (S22b F5)


async def test_flipkart_refresh_flag_off_keeps_the_listing_due(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S22b F5 (moved from the tester's repro): a refresh-status accepted while the flag is off
    must not drop the listing's sync schedule; once the flag is on the scheduler enqueues it."""
    from app.workers.periodic import schedule_status_sync

    made = await _fk(fkp)
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    await run_job(uuid.UUID(item["job_id"]))
    assert fkp.pub.listing(made.listing_id)["status"] == "published"
    flag(monkeypatch, False)
    refresh = await api.post(
        f"/api/v1/listings/{made.listing_id}/refresh-status",
        headers=fkp.world.headers(OPERATOR),
    )
    assert refresh.status_code == 202, refresh.text
    await run_job(uuid.UUID(refresh.json()["job_id"]))
    assert fkp.pub.listing(made.listing_id)["next_sync_at"] is not None
    await schedule_status_sync()
    assert fkp.jobs(made.listing_id, "listing_status_sync") == 1  # still off: nothing new
    flag(monkeypatch, True)
    await schedule_status_sync()
    assert fkp.jobs(made.listing_id, "listing_status_sync") == 2
