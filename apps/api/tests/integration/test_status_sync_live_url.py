"""S26 T21 (AC 9; spec "Listing ID and URL are stored"; DATABASE.md:757, :842): a status sync
that confirms a listing live stores the adapter-built product page URL on each live item and on
the listing, which `GET /listings/{id}` returns. Found by the acceptance run: a live listing came
back with `live_url` null (nothing wrote the column)."""

from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, pub, publish_and_run, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


async def _sync(pub: PubWorld, listing_id: uuid.UUID) -> None:
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


async def test_status_sync_live_stores_the_live_url(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    sku = str(
        pub.scalar("SELECT seller_sku FROM listing_items WHERE listing_id = %s", [made.listing_id])
    )
    pub.fake.read_states[sku] = "live"
    await _sync(pub, made.listing_id)
    ((external, item_url),) = pub.rows(
        "SELECT external_id, live_url FROM listing_items WHERE listing_id = %s", [made.listing_id]
    )
    assert external and item_url == f"https://www.amazon.in/dp/{external}"
    row = pub.listing(made.listing_id)
    assert row["marketplace_state"] == "live" and row["live_url"] == item_url
    response = await api.get(
        f"/api/v1/listings/{made.listing_id}", headers=pub.world.headers("operator@acme.test")
    )
    assert response.status_code == 200
    assert response.json()["live_url"] == item_url


async def test_status_sync_processing_stores_no_url(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    sku = str(
        pub.scalar("SELECT seller_sku FROM listing_items WHERE listing_id = %s", [made.listing_id])
    )
    pub.fake.read_states[sku] = "processing"
    await _sync(pub, made.listing_id)
    ((item_url,),) = pub.rows(
        "SELECT live_url FROM listing_items WHERE listing_id = %s", [made.listing_id]
    )
    assert item_url is None and pub.listing(made.listing_id)["live_url"] is None
