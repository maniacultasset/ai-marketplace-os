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
