"""S21 T21, T22 (DW7; AC10, AC14; D7; CP5): the `listing_status_sync` job over the reader, its
writes, and the per-minute scheduler."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, pub, publish_and_run, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


def _sku(pub: PubWorld, listing_id: uuid.UUID) -> str:
    return str(
        pub.scalar("SELECT seller_sku FROM listing_items WHERE listing_id = %s", [listing_id])
    )


def _due(pub: PubWorld, listing_id: uuid.UUID) -> None:
    pub.conn.execute(
        "UPDATE listings SET next_sync_at = now() - interval '1 second' WHERE id = %s",
        [listing_id],
    )


async def _sync(pub: PubWorld, listing_id: uuid.UUID) -> Any:
    """One scheduler pass for the due listing, then its sync job."""
    from app.workers.periodic import schedule_status_sync

    _due(pub, listing_id)
    assert await schedule_status_sync() >= 1
    job = pub.scalar(
        "SELECT id FROM job_runs WHERE listing_id = %s AND kind = 'listing_status_sync' "
        "AND status = 'queued'",
        [listing_id],
    )
    assert job is not None
    assert pub.listing(listing_id)["next_sync_at"] is None
    await run_job(job)
    return job


async def test_published_not_live_until_sync(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    response = await api.get(
        f"/api/v1/listings/{made.listing_id}", headers=pub.world.headers("operator@acme.test")
    )
    body = response.json()
    assert (body["status"], body["marketplace_state"], body["live_url"]) == (
        "published",
        "processing",
        None,
    )


async def test_status_sync_live(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    pub.fake.read_states[_sku(pub, made.listing_id)] = "live"
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["marketplace_state"], row["live_version_id"]) == (
        "published",
        "live",
        made.version_id,
    )
    assert row["live_confirmed_at"] is not None and row["last_synced_at"] is not None
    assert row["last_event"] == "sync" and row["next_sync_at"] is not None
    item = pub.rows(
        "SELECT marketplace_state, live_version_id, external_id FROM listing_items "
        "WHERE listing_id = %s",
        [made.listing_id],
    )
    assert item[0][0] == "live" and item[0][1] == made.version_id and item[0][2]
    (sub,) = pub.submissions(made.listing_id)
    assert sub["outcome"] == "live"


async def test_status_sync_processing_counts_then_exhausts(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """The 9th counted poll with no terminal state: `sync_exhausted` + the submission `error
    MARKETPLACE_UNCONFIRMED` + no next slot; then one publish makes exactly one attempt."""
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    pub.fake.read_states[_sku(pub, made.listing_id)] = "processing"
    before = pub.listing(made.listing_id)["last_event_at"]
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["sync_poll_count"], row["last_event_at"]) == (1, before)  # write economy
    pub.conn.execute("UPDATE listings SET sync_poll_count = 8 WHERE id = %s", [made.listing_id])
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"], row["last_event"], row["next_sync_at"]) == (
        "blocked",
        "marketplace_action_required",
        "sync_exhausted",
        None,
    )
    (sub,) = pub.submissions(made.listing_id)
    assert (sub["outcome"], sub["error_code"]) == ("error", "MARKETPLACE_UNCONFIRMED")
    calls = len(pub.fake.calls)
    await publish_and_run(api, pub, made)
    subs = pub.submissions(made.listing_id)
    assert [(s["attempt"], s["outcome"]) for s in subs] == [(1, "error"), (2, "accepted")]
    assert subs[1]["retry_justification"] == "retryable_condition"
    assert len(pub.fake.calls) == calls + 1


async def test_status_sync_rejected_failed(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    pub.fake.read_states[_sku(pub, made.listing_id)] = "rejected"
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "marketplace_rejected")
    assert pub.submissions(made.listing_id)[0]["outcome"] == "rejected"


async def test_status_sync_removed_blocked(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    sku = _sku(pub, made.listing_id)
    pub.fake.read_states[sku] = "live"
    await _sync(pub, made.listing_id)
    pub.fake.read_states[sku] = "gone"
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"], row["marketplace_state"]) == (
        "blocked",
        "marketplace_action_required",
        "removed",
    )


async def test_status_sync_partial_listing_items_synced(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """D7: a `failed/publish_partial` listing's items are synced; its status stays."""
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": ("T", "t", ErrorCode.RATE_LIMITED)})
    await publish_and_run(api, pub, made)
    pub.fake.read_states[_sku(pub, made.listing_id)] = "live"
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"], row["marketplace_state"]) == (
        "failed",
        "publish_partial",
        "live",
    )


async def test_status_sync_slot_fires_once_two_workers(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """Two scheduler passes over one due slot enqueue one job (the first sets `next_sync_at`
    NULL in the enqueue transaction)."""
    import asyncio

    from app.workers.periodic import schedule_status_sync

    made = await approved(pub)
    await publish_and_run(api, pub, made)
    _due(pub, made.listing_id)
    results = await asyncio.gather(schedule_status_sync(), schedule_status_sync())
    assert sum(results) == 1
    assert (
        pub.scalar(
            "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_status_sync'",
            [made.listing_id],
        )
        == 1
    )


@pytest.mark.parametrize("column", ["paused_at", "archived_at"])
async def test_status_sync_paused_archived_never(
    api: httpx.AsyncClient, pub: PubWorld, column: str
) -> None:
    from app.workers.periodic import schedule_status_sync

    made = await approved(pub)
    await publish_and_run(api, pub, made)
    _due(pub, made.listing_id)
    sql = f"UPDATE listings SET {column} = now() WHERE id = %s"  # noqa: S608
    pub.conn.execute(sql, [made.listing_id])
    assert await schedule_status_sync() == 0


async def test_status_sync_suspended_tenant_never(api: httpx.AsyncClient, pub: PubWorld) -> None:
    from app.workers.periodic import schedule_status_sync

    made = await approved(pub)
    await publish_and_run(api, pub, made)
    _due(pub, made.listing_id)
    with pub.conn.transaction():
        pub.conn.execute("SET LOCAL session_replication_role = replica")
        pub.conn.execute(
            "UPDATE tenants SET status = 'suspended' WHERE id = %s", [pub.world.tenant("acme")]
        )
    assert await schedule_status_sync() == 0


async def test_status_sync_job_no_user_audit_ok(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """A scheduled sync has no `created_by`: its writes run with no user GUC."""
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    pub.fake.read_states[_sku(pub, made.listing_id)] = "live"
    job = await _sync(pub, made.listing_id)
    row = pub.job(job)
    assert (row["created_by"], row["status"]) == (None, "succeeded")


async def test_status_sync_dead_letter_counts(api: httpx.AsyncClient, pub: PubWorld) -> None:
    from app.adapters.base import MarketplaceUnavailable
    from mkos_schemas.errors import ErrorReport

    made = await approved(pub)
    await publish_and_run(api, pub, made)

    async def down(*a: Any, **k: Any) -> Any:
        raise MarketplaceUnavailable(ErrorReport(status="failed"))

    pub.fake.get_listing = down
    from app.workers.periodic import schedule_status_sync

    _due(pub, made.listing_id)
    await schedule_status_sync()
    job = pub.scalar(
        "SELECT id FROM job_runs WHERE listing_id = %s AND kind = 'listing_status_sync'",
        [made.listing_id],
    )
    for _ in range(3):
        try:
            await run_job(job)
        except Exception as exc:
            assert type(exc).__name__ == "RetryLater"
    assert pub.job(job)["status"] == "dead_letter"
    row = pub.listing(made.listing_id)
    assert row["sync_poll_count"] == 1 and row["next_sync_at"] is not None
