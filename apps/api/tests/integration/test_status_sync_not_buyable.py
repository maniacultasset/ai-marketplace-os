"""SI9 T6 (AC4, AC5, AC7; D3; CP-D7, CP-D9): a published listing the marketplace reads as on the
marketplace but not buyable stays `published/inactive`, gets its url and id, keeps being
re-checked (bounded gaps, then daily, never exhausted) and turns live on a later buyable read."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, pub, publish_and_run, run_job
from .review_support import OPERATOR

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


def _sku(pub: PubWorld, listing_id: uuid.UUID) -> str:
    return str(
        pub.scalar("SELECT seller_sku FROM listing_items WHERE listing_id = %s", [listing_id])
    )


async def _sync(pub: PubWorld, listing_id: uuid.UUID) -> Any:
    from app.workers.periodic import schedule_status_sync

    pub.conn.execute(
        "UPDATE listings SET next_sync_at = now() - interval '1 second' WHERE id = %s",
        [listing_id],
    )
    assert await schedule_status_sync() >= 1
    job = pub.scalar(
        "SELECT id FROM job_runs WHERE listing_id = %s AND kind = 'listing_status_sync' "
        "AND status = 'queued'",
        [listing_id],
    )
    assert job is not None
    await run_job(job)
    return job


def _gap(pub: PubWorld, listing_id: uuid.UUID) -> timedelta:
    value = pub.scalar("SELECT next_sync_at - now() FROM listings WHERE id = %s", [listing_id])
    assert isinstance(value, timedelta)
    return value


async def _not_buyable(api: httpx.AsyncClient, pub: PubWorld) -> Any:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    pub.fake.read_states[_sku(pub, made.listing_id)] = "not_buyable"
    await _sync(pub, made.listing_id)
    return made


async def test_not_buyable_stays_published(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await _not_buyable(api, pub)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"], row["marketplace_state"]) == (
        "published",
        None,
        "inactive",
    )
    assert row["external_id"] and str(row["live_url"]).startswith("https://")
    assert row["live_confirmed_at"] is None and row["live_version_id"] is None
    assert row["next_sync_at"] is not None and row["sync_poll_count"] == 1
    assert row["last_event"] == "sync"
    items = pub.rows(
        "SELECT marketplace_state, external_id, live_url, live_version_id FROM listing_items "
        "WHERE listing_id = %s",
        [made.listing_id],
    )
    assert [(i[0], bool(i[1]), str(i[2]).startswith("https://"), i[3]) for i in items] == [
        ("inactive", True, True, None)
    ]
    (sub,) = pub.submissions(made.listing_id)
    assert sub["outcome"] == "accepted"


async def test_not_buyable_never_exhausts_then_daily(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await _not_buyable(api, pub)
    for _ in range(10):
        await _sync(pub, made.listing_id)
        row = pub.listing(made.listing_id)
        assert (row["status"], row["marketplace_state"]) == ("published", "inactive")
    assert pub.listing(made.listing_id)["sync_poll_count"] == 9
    assert timedelta(hours=23) < _gap(pub, made.listing_id) <= timedelta(hours=24)
    (sub,) = pub.submissions(made.listing_id)
    assert sub["outcome"] == "accepted"


async def test_not_buyable_then_buyable_goes_live(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await _not_buyable(api, pub)
    pub.fake.read_states[_sku(pub, made.listing_id)] = "live"
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["marketplace_state"], row["live_version_id"]) == (
        "published",
        "live",
        made.version_id,
    )
    assert row["live_confirmed_at"] is not None
    (sub,) = pub.submissions(made.listing_id)
    assert sub["outcome"] == "live"


async def test_resync_same_state_writes_only_the_schedule(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await _not_buyable(api, pub)
    before = pub.listing(made.listing_id)
    await _sync(pub, made.listing_id)
    after = pub.listing(made.listing_id)
    assert after["last_event_at"] == before["last_event_at"]
    assert (after["marketplace_state"], after["live_url"], after["external_id"]) == (
        before["marketplace_state"],
        before["live_url"],
        before["external_id"],
    )
    assert after["sync_poll_count"] == before["sync_poll_count"] + 1


async def test_dead_letter_on_not_buyable_reschedules(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    from app.adapters.base import MarketplaceUnavailable
    from app.workers.periodic import schedule_status_sync
    from mkos_schemas.errors import ErrorReport

    made = await _not_buyable(api, pub)
    pub.conn.execute("UPDATE listings SET sync_poll_count = 9 WHERE id = %s", [made.listing_id])

    async def down(*a: Any, **k: Any) -> Any:
        raise MarketplaceUnavailable(ErrorReport(status="failed"))

    pub.fake.get_listing = down
    pub.conn.execute(
        "UPDATE listings SET next_sync_at = now() - interval '1 second' WHERE id = %s",
        [made.listing_id],
    )
    await schedule_status_sync()
    job = pub.scalar(
        "SELECT id FROM job_runs WHERE listing_id = %s AND kind = 'listing_status_sync' "
        "AND status = 'queued'",
        [made.listing_id],
    )
    for _ in range(3):
        try:
            await run_job(job)
        except Exception as exc:
            assert type(exc).__name__ == "RetryLater"
    assert pub.job(job)["status"] == "dead_letter"
    row = pub.listing(made.listing_id)
    assert (row["status"], row["marketplace_state"], row["sync_poll_count"]) == (
        "published",
        "inactive",
        9,
    )
    assert timedelta(hours=23) < _gap(pub, made.listing_id) <= timedelta(hours=24)


async def test_refresh_status_and_external_id_on_get(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await _not_buyable(api, pub)
    url = f"/api/v1/listings/{made.listing_id}/refresh-status"
    first = await api.post(url, headers=pub.world.headers(OPERATOR))
    assert first.status_code == 202, first.text
    await run_job(uuid.UUID(first.json()["job_id"]))
    assert pub.listing(made.listing_id)["marketplace_state"] == "inactive"
    second = await api.post(url, headers=pub.world.headers(OPERATOR))
    assert second.status_code == 429, second.text
    got = await api.get(f"/api/v1/listings/{made.listing_id}", headers=pub.world.headers(OPERATOR))
    body = got.json()
    assert (body["status"], body["marketplace_state"]) == ("published", "inactive")
    assert body["external_id"] == pub.listing(made.listing_id)["external_id"]
    assert body["live_url"].startswith("https://")


async def test_not_buyable_at_nine_then_processing_blocks(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await _not_buyable(api, pub)
    pub.conn.execute("UPDATE listings SET sync_poll_count = 9 WHERE id = %s", [made.listing_id])
    pub.fake.read_states[_sku(pub, made.listing_id)] = "processing"
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"], row["sync_poll_count"]) == (
        "blocked",
        "marketplace_action_required",
        9,
    )
