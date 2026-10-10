"""S22b tester attacks (AC17, AC18): the Flipkart flag around idempotency keys, roles, duplicate
job delivery and read-only routes, and the parentless family's ledger through a field retry."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .flipkart_publish_support import FkWorld, fkp, flag, pub
from .publish_support import approved, post_publish, retry, run_job
from .review_support import OPERATOR, REVIEWER

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["fkp", "pub"]
QUOTA = ("FAKE-FK-QUOTA", "Request throttled")


async def _family(fk: FkWorld) -> Any:
    return await approved(fk.pub, sku="KURTA-FAM-01", marketplace="flipkart")


async def _partial(api: httpx.AsyncClient, fk: FkWorld, made: Any) -> None:
    fk.app.state.fake.fk_reject_attribute["title"] = QUOTA
    response = await post_publish(api, fk.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    assert item["http_status"] == 202, item
    await run_job(uuid.UUID(item["job_id"]))
    fk.app.state.fake.fk_reject_attribute.clear()
    row = fk.pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "publish_partial")


async def test_flipkart_retry_fields_flag_off_key_reusable_once_on(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag-off 501 stores no answer under the key: the same key, once the flag is on,
    runs the retry (one job) and then replays that 202."""
    made = await _family(fkp)
    await _partial(api, fkp, made)
    key = str(uuid.uuid4())
    calls = len(fkp.calls())
    flag(monkeypatch, False)
    off = await retry(api, fkp.pub, made, key=key)
    assert off.status_code == 501, off.text
    assert fkp.jobs(made.listing_id) == 1 and len(fkp.calls()) == calls
    assert len(fkp.pub.submissions(made.listing_id)) == 1
    flag(monkeypatch, True)
    on = await retry(api, fkp.pub, made, key=key)
    assert on.status_code == 202, on.text
    again = await retry(api, fkp.pub, made, key=key)
    assert again.status_code == 202 and again.content == on.content
    assert fkp.jobs(made.listing_id) == 2


async def test_flipkart_flag_off_role_checked_before_flag(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer may not publish: 403 whether or not Flipkart is on (the flag never answers
    for a caller without the permission)."""
    made = await approved(fkp.pub, marketplace="flipkart")
    flag(monkeypatch, False)
    response = await post_publish(api, fkp.pub, REVIEWER, [{"listing_id": str(made.listing_id)}])
    assert response.status_code == 403, response.text
    response = await retry(api, fkp.pub, made, email=REVIEWER)
    assert response.status_code == 403, response.text
    assert fkp.jobs(made.listing_id) == 0


async def test_flipkart_job_claimed_flag_off_redelivered(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate delivery of a job refused while off: the second run writes and sends nothing
    and the listing stays `failed/publish_failed`."""
    made = await approved(fkp.pub, marketplace="flipkart")
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    flag(monkeypatch, False)
    await run_job(uuid.UUID(item["job_id"]))
    first = fkp.pub.listing(made.listing_id)
    flag(monkeypatch, True)  # even with the flag back on, the ended job does not run again
    await run_job(uuid.UUID(item["job_id"]))
    after = fkp.pub.listing(made.listing_id)
    assert (after["status"], after["status_reason"]) == ("failed", "publish_failed")
    assert after["last_event"] == first["last_event"] == "submission_failed"
    assert fkp.pub.submissions(made.listing_id) == []
    assert fkp.calls() == [] and fkp.opened == []
    assert fkp.pub.job(item["job_id"])["status"] == "failed"


async def test_flipkart_flag_off_reads_still_work(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag gates outbound work only: a published Flipkart listing's submissions stay
    readable while off, and nothing is sent."""
    made = await _family(fkp)
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    await run_job(uuid.UUID(item["job_id"]))
    calls = len(fkp.calls())
    flag(monkeypatch, False)
    page = await api.get(
        f"/api/v1/listings/{made.listing_id}/submissions", headers=fkp.world.headers(OPERATOR)
    )
    assert page.status_code == 200, page.text
    assert len(page.json()["data"]) == 1, page.json()
    listing = await api.get(
        f"/api/v1/listings/{made.listing_id}", headers=fkp.world.headers(OPERATOR)
    )
    assert listing.status_code == 200, listing.text
    assert len(fkp.calls()) == calls


async def test_flipkart_family_retry_keeps_ledger_nulls(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    """After a partial create and a field retry the parentless family still has no
    listing-level SKU or external id and exactly its 4 child items (no parent row appears on
    the second attempt, DATABASE.md:754-755)."""
    made = await _family(fkp)
    await _partial(api, fkp, made)
    response = await retry(api, fkp.pub, made)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    row = fkp.pub.listing(made.listing_id)
    assert (row["status"], row["marketplace_sku"], row["external_id"]) == ("published", None, None)
    items = fkp.pub.rows(
        "SELECT item_role, external_id FROM listing_items WHERE listing_id = %s",
        [made.listing_id],
    )
    assert sorted(role for role, _ in items) == ["child"] * 4
    assert all(ext is not None for _, ext in items)
    subs = fkp.pub.submissions(made.listing_id)
    assert [(s["attempt"], s["operation"]) for s in subs] == [(1, "create"), (2, "patch")]
