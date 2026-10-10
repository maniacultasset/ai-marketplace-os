"""S22 T22 (AC18; invariants 1, 10; C9): the Flipkart publish loop with `FLIPKART_ENABLED=true`,
through S21's job, ledger, field retry and status sync, with the real `FlipkartAdapter` over the
in-process fake Flipkart listings API. KURTA-FAM-01 is the seeded 2 x 2 family: on Flipkart it
has no parent row, so the job sends (and the ledger keeps) its 4 children only."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .flipkart_publish_support import FkWorld, fkp, pub
from .publish_support import approved, post_publish, retry, run_job
from .review_support import OPERATOR, Made

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["fkp", "pub"]
FAMILY = "KURTA-FAM-01"
QUOTA = ["FAKE-FK-QUOTA", "Request throttled"]
# the two paths the fake refuses, and the Flipkart attributes they feed
FAILED_PATHS = ("content.description", "content.title")
FAILED_ATTRIBUTES = {"description", "title"}


async def _family(fk: FkWorld) -> Made:
    return await approved(fk.pub, sku=FAMILY, marketplace="flipkart")


def _items(fk: FkWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return fk.pub.rows(
        "SELECT seller_sku, item_role, external_id, marketplace_state FROM listing_items "
        "WHERE listing_id = %s ORDER BY seller_sku",
        [listing_id],
    )


async def _publish(api: httpx.AsyncClient, fk: FkWorld, made: Made, key: str | None = None) -> Any:
    response = await post_publish(
        api, fk.pub, OPERATOR, [{"listing_id": str(made.listing_id)}], key=key
    )
    assert response.status_code == 202, response.text
    return response


async def _publish_run(api: httpx.AsyncClient, fk: FkWorld, made: Made) -> dict[str, Any]:
    (item,) = (await _publish(api, fk, made)).json()["items"]
    assert item["http_status"] == 202, item
    await run_job(uuid.UUID(item["job_id"]))
    return item  # type: ignore[no-any-return]


async def _partial(api: httpx.AsyncClient, fk: FkWorld, made: Made) -> dict[str, Any]:
    """Publish with the fake refusing `title` and `description` on every SKU (retryable)."""
    for name in FAILED_ATTRIBUTES:
        fk.app.state.fake.fk_reject_attribute[name] = tuple(QUOTA)
    item = await _publish_run(api, fk, made)
    fk.app.state.fake.fk_reject_attribute.clear()
    return item


def _sent_attributes(call: dict[str, Any]) -> dict[str, set[str]]:
    return {e["sku"]: set(e["attributes"]) for e in json.loads(call["body"])["listings"]}


async def test_flipkart_publish_partial_retry(api: httpx.AsyncClient, fkp: FkWorld) -> None:
    """Approved family → 202 → job `partial` with 2 failed fields → retry-fields sends only
    those 2 → sync → `live`."""
    made = await _family(fkp)
    item = await _partial(api, fkp, made)
    assert fkp.pub.job(item["job_id"])["status"] == "partial"
    row = fkp.pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "publish_partial")
    (sub,) = fkp.pub.submissions(made.listing_id)
    assert (sub["attempt"], sub["operation"], sub["outcome"]) == (1, "create", "partially_accepted")
    failed = fkp.pub.rows(
        "SELECT DISTINCT field FROM publish_field_results WHERE submission_id = %s "
        "AND status = 'failed' ORDER BY field",
        [sub["id"]],
    )
    assert tuple(f for (f,) in failed) == FAILED_PATHS
    skus = [sku for sku, *_ in _items(fkp, made.listing_id)]
    assert len(skus) == 4

    before = len(fkp.calls())
    response = await retry(api, fkp.pub, made)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    sent = fkp.calls()[before:]
    assert sent and all(c["path"].endswith("/listings/v3/update") for c in sent)
    attributes: dict[str, set[str]] = {}
    for call in sent:
        attributes.update(_sent_attributes(call))
    assert attributes == dict.fromkeys(skus, FAILED_ATTRIBUTES)  # invariant 10
    subs = fkp.pub.submissions(made.listing_id)
    assert [(s["attempt"], s["operation"], s["outcome"]) for s in subs] == [
        (1, "create", "partially_accepted"),
        (2, "patch", "accepted"),
    ]
    assert fkp.pub.listing(made.listing_id)["status"] == "published"

    await fkp.control(fk_state=dict.fromkeys(skus, "ACTIVE"))
    fkp.pub.conn.execute(
        "UPDATE listings SET next_sync_at = now() - interval '1 second' WHERE id = %s",
        [made.listing_id],
    )
    from app.workers.periodic import schedule_status_sync

    assert await schedule_status_sync() >= 1
    sync = fkp.pub.scalar(
        "SELECT id FROM job_runs WHERE listing_id = %s AND kind = 'listing_status_sync' "
        "AND status = 'queued'",
        [made.listing_id],
    )
    await run_job(sync)
    row = fkp.pub.listing(made.listing_id)
    assert (row["status"], row["marketplace_state"]) == ("published", "live")
    assert {state for *_, state in _items(fkp, made.listing_id)} == {"live"}


async def test_flipkart_publish_family_ledger_nulls(api: httpx.AsyncClient, fkp: FkWorld) -> None:
    """A parentless family: no listing-level SKU or external id, 4 child items with their own
    Flipkart listing ids, no parent item (DATABASE.md:754-755)."""
    made = await _family(fkp)
    await _publish_run(api, fkp, made)
    row = fkp.pub.listing(made.listing_id)
    assert (row["status"], row["marketplace_sku"], row["external_id"]) == ("published", None, None)
    items = _items(fkp, made.listing_id)
    assert [role for _, role, *_ in items] == ["child"] * 4
    stored = {sku: entry["listing_id"] for (_, sku), entry in fkp.app.state.fake.fk_items.items()}
    assert {sku: ext for sku, _, ext, _ in items} == {sku: stored[sku] for sku, *_ in items}
    (call,) = fkp.calls()
    assert {e["sku"] for e in json.loads(call["body"])["listings"]} == {s for s, *_ in items}


async def test_flipkart_publish_idempotency(api: httpx.AsyncClient, fkp: FkWorld) -> None:
    made = await _family(fkp)
    key = str(uuid.uuid4())
    first = await _publish(api, fkp, made, key)
    again = await _publish(api, fkp, made, key)
    assert first.content == again.content
    assert fkp.jobs(made.listing_id) == 1
    other = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(uuid.uuid4())}], key=key)
    assert other.status_code == 409 and other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    (item,) = first.json()["items"]
    await run_job(uuid.UUID(item["job_id"]))
    calls = len(fkp.calls())
    replay = await _publish(api, fkp, made, key)  # after the job: still the stored answer
    assert replay.content == first.content and len(fkp.calls()) == calls


async def test_flipkart_publish_submission_cap_flipkart(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    """3 retryable failures of one version; the 4th publish is refused and the listing is
    `blocked/publish_attempts_exhausted` (invariant 9)."""
    made = await _family(fkp)
    for attempt in (1, 2, 3):
        for name in FAILED_ATTRIBUTES:
            fkp.app.state.fake.fk_reject_attribute[name] = tuple(QUOTA)
        if attempt == 1:
            await _publish_run(api, fkp, made)
        else:
            response = await retry(api, fkp.pub, made)
            assert response.status_code == 202, (attempt, response.text)
            await run_job(uuid.UUID(response.json()["job_id"]))
    fkp.app.state.fake.fk_reject_attribute.clear()
    subs = fkp.pub.submissions(made.listing_id)
    assert [s["attempt"] for s in subs] == [1, 2, 3]
    row = fkp.pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "publish_attempts_exhausted")
    calls = len(fkp.calls())
    (item,) = (await _publish(api, fkp, made)).json()["items"]
    assert (item["http_status"], item["error"]["error_code"]) == (
        409,
        "SUBMISSION_ATTEMPTS_EXHAUSTED",
    )
    response = await retry(api, fkp.pub, made)
    assert response.status_code == 409
    assert len(fkp.calls()) == calls and len(fkp.pub.submissions(made.listing_id)) == 3


async def test_flipkart_publish_raw_response_stored(api: httpx.AsyncClient, fkp: FkWorld) -> None:
    """The job's sink reaches the Flipkart adapter: the raw bodies are stored once, and the
    submission's `submission_ref` is Flipkart's `request_id`."""
    from app.adapters.storage import get_storage, submissions_bucket

    made = await _family(fkp)
    await _publish_run(api, fkp, made)
    (sub,) = fkp.pub.submissions(made.listing_id)
    (call,) = fkp.calls()
    assert sub["raw_response_storage_key"] is not None
    assert sub["raw_response_sha256"] is not None
    raw = await get_storage().get(submissions_bucket(), sub["raw_response_storage_key"])
    lines = [json.loads(line) for line in raw.decode().splitlines() if line]
    assert {line["seller_sku"] for line in lines} == {s for s, *_ in _items(fkp, made.listing_id)}
    assert all(line["operation"] == "create" for line in lines)
    request_ids = {json.loads(line["body"])["request_id"] for line in lines}
    assert len(request_ids) == 1
    assert sub["submission_ref"] == request_ids.pop()
    assert call["path"].endswith("/listings/v3")
