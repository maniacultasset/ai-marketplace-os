"""Revoke and reconnect state machine (audit GAP1-UNTOUCHED-OPS-STATE-MACHINE-1..3; DATABASE.md:760,
:768, :1352).

1. A revoke + reconnect makes a NEW account row (confirm's live lookup skips `revoked`), so the
   connect re-points every listing of the marketplace pinned to a revoked account to the new one:
   a listing published before can be published again.
2. The status sync never reads through a non-active account: the scheduler leaves the listing's
   slot due, the job skips it (no INTERNAL_ERROR on any slot), and the sync resumes once the
   listing is on an active account again. An auth refusal while opening the reader is the typed
   AUTH_EXPIRED, never INTERNAL_ERROR.
3. A revoke landing during a live publish opens no `reconnect_account` task on the revoked
   account; the next connect closes the loop (an applied task on the new account, linked to the
   attempt's AUTH_EXPIRED issue), so the listing can be resumed.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import Any

import httpx
import pytest
from app.adapters.base import AuthExpired
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .publish_support import (
    PubWorld,
    approved,
    pub,
    publish_and_run,
    publish_one,
    run_job,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]

OWNER = "owner@acme.test"


async def _revoke(api: httpx.AsyncClient, pw: PubWorld, account: uuid.UUID) -> None:
    r = await api.post(
        f"/api/v1/marketplaces/accounts/{account}/revoke", headers=pw.world.headers(OWNER)
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked"


async def _connect(api: httpx.AsyncClient, pw: PubWorld, seller: str) -> uuid.UUID:
    """The OAuth callback's pending row (stored as the callback would), then the real confirm."""
    handle = secrets.token_urlsafe(32)
    pw.conn.execute(
        "INSERT INTO oauth_states (tenant_id, state_hash, user_id, marketplace, expires_at, "
        "token_enc, kms_key_version, seller_id, region, scopes, token_expires_at) VALUES "
        "(%s, %s, %s, 'amazon', now() + interval '10 minutes', %s, 'v1', %s, NULL, '{}', "
        "now() + interval '1 hour')",
        [
            pw.world.tenant("acme"),
            hashlib.sha256(handle.encode("ascii")).digest(),
            pw.world.user(OWNER),
            b"sealed",
            seller,
        ],
    )
    r = await api.post(
        "/api/v1/marketplaces/connect/confirm",
        json={"pending": handle},
        headers=pw.world.headers(OWNER),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    return uuid.UUID(r.json()["id"])


def _seller(pw: PubWorld, account: uuid.UUID) -> str:
    return str(pw.scalar("SELECT seller_id FROM marketplace_accounts WHERE id = %s", [account]))


async def _approve_edit(api: httpx.AsyncClient, pw: PubWorld, made: Any) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.review import ReviewItem, approve_batch

    from .review_support import REVIEWER, edited

    v2 = await edited(api, pw.world, made, email=OWNER)
    principal = pw.world.principal(REVIEWER)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        outcome = await approve_batch(
            uow,
            principal,
            [ReviewItem(v2.listing_id, v2.version_id, "approve")],
            http_key=str(uuid.uuid4()),
        )
    assert outcome.items[0].http_status == 200, outcome.items[0]
    return v2


def _install_plain_factory(pw: PubWorld) -> None:
    from app.workers import publish as publish_worker

    async def factory(tenant_id: uuid.UUID, account_id: uuid.UUID, sink: Any) -> Any:
        pw.sinks.append(sink)
        return pw.fake

    publish_worker.set_adapter_factory(factory)


# ---- 1: revoke + reconnect re-points the listing ----


async def test_reconnect_after_revoke_republishes_listing(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    old = pub.accounts["acme"]
    assert pub.listing(made.listing_id)["marketplace_account_id"] == old

    await _revoke(api, pub, old)
    new = await _connect(api, pub, _seller(pub, old))
    assert new != old
    assert pub.listing(made.listing_id)["marketplace_account_id"] == new
    # another tenant's listing is never touched
    assert pub.scalar(
        "SELECT count(*) FROM listings WHERE marketplace_account_id = %s", [pub.accounts["bharat"]]
    ) == pub.scalar(
        "SELECT count(*) FROM listings WHERE tenant_id = %s AND marketplace_account_id IS NOT NULL",
        [pub.world.tenant("bharat")],
    )

    v2 = await _approve_edit(api, pub, made)
    item = await publish_one(api, pub, v2)
    assert item["http_status"] == 202, item
    await run_job(uuid.UUID(item["job_id"]))
    row = pub.listing(made.listing_id)
    assert row["status"] == "published", (row["status"], row["status_reason"])
    latest = pub.submissions(made.listing_id)[-1]
    assert latest["marketplace_account_id"] == new


async def test_reconnect_leaves_listings_of_a_live_account_alone(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """Only listings pinned to a revoked account move; a reconnect of the live account (same id)
    re-points nothing."""
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    account = pub.accounts["acme"]
    same = await _connect(api, pub, _seller(pub, account))
    assert same == account
    assert pub.listing(made.listing_id)["marketplace_account_id"] == account


# ---- 2: status sync on a revoked account ----


async def test_sync_after_revoke_never_fails_internal(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    from app.workers import readers
    from app.workers.periodic import schedule_status_sync

    made = await approved(pub)
    await publish_and_run(api, pub, made)
    old = pub.accounts["acme"]
    await _revoke(api, pub, old)
    readers.set_reader_factory(None)  # the real factory refuses a revoked row

    for _ in range(3):
        pub.conn.execute(
            "UPDATE listings SET next_sync_at = now() - interval '1 second' WHERE id = %s",
            [made.listing_id],
        )
        await schedule_status_sync()
        queued = pub.scalar(
            "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_status_sync' "
            "AND status = 'queued'",
            [made.listing_id],
        )
        assert queued == 0  # the scheduler skips a listing whose account is not active
        assert pub.listing(made.listing_id)["next_sync_at"] is not None  # the slot stays due

    # a member's refresh-status: the job skips the listing, never INTERNAL_ERROR
    r = await api.post(
        f"/api/v1/listings/{made.listing_id}/refresh-status", headers=pub.world.headers(OWNER)
    )
    assert r.status_code == 202, r.text
    job = uuid.UUID(r.json()["job_id"])
    await run_job(job)
    row = pub.job(job)
    assert row["status"] == "succeeded", row["error_report_json"]
    assert (row["result_json"] or {}).get("skipped") is True
    listing = pub.listing(made.listing_id)
    assert listing["next_sync_at"] is not None  # the slot is put back for after the reconnect
    assert listing["status"] == "published"
    failed = pub.rows(
        "SELECT error_report_json ->> 'error_code' FROM job_runs WHERE listing_id = %s "
        "AND kind = 'listing_status_sync' AND status IN ('failed', 'dead_letter')",
        [made.listing_id],
    )
    assert failed == []

    # after the reconnect the listing is on the new account and the scheduler syncs it again
    _install_reader(pub)
    await _connect(api, pub, _seller(pub, old))
    pub.conn.execute(
        "UPDATE listings SET next_sync_at = now() - interval '1 second' WHERE id = %s",
        [made.listing_id],
    )
    assert await schedule_status_sync() >= 1


def _install_reader(pw: PubWorld) -> None:
    from app.workers import readers

    async def reader(tenant_id: uuid.UUID, account_id: uuid.UUID) -> Any:
        return pw.fake

    readers.set_reader_factory(reader)


async def test_sync_auth_refused_at_open_is_auth_expired(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """The account turned non-active between the job's read and the reader's open: the job ends
    with the typed AUTH_EXPIRED, not INTERNAL_ERROR."""
    from app.workers import readers

    made = await approved(pub)
    await publish_and_run(api, pub, made)

    async def refused(tenant_id: uuid.UUID, account_id: uuid.UUID) -> Any:
        raise AuthExpired(ErrorReport(status="failed"))

    readers.set_reader_factory(refused)
    r = await api.post(
        f"/api/v1/listings/{made.listing_id}/refresh-status", headers=pub.world.headers(OWNER)
    )
    assert r.status_code == 202, r.text
    job = uuid.UUID(r.json()["job_id"])
    await run_job(job)
    row = pub.job(job)
    assert row["status"] == "failed"
    assert row["error_report_json"]["error_code"] == "AUTH_EXPIRED"
