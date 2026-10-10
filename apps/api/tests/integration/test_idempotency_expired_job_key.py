"""Audit CONCURRENCY-1 and CONCURRENCY-2: "a key lives 24 h; an expired key counts as new"
(repositories/idempotency.py). The job key derived from an HTTP key is scoped to the key's
lifetime, so reusing an expired key on refresh-status, regenerate or publish queues a fresh job.
It never returns the old finished job as `queued`, never takes the listing off the sync
schedule, and is never refused 409 LISTING_BUSY when nothing is live."""

from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import set_status
from .publish_support import PubWorld, approved, post_publish, pub, publish_and_run
from .review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    edited,
    generated,
    item,
    post_approve,
    run_job,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


def _expire(pw: PubWorld, key: str) -> None:
    """The key's 24 h are over (the hourly prune has not run yet)."""
    pw.conn.execute(
        "UPDATE idempotency_keys SET expires_at = now() - interval '1 second' WHERE key = %s",
        [key],
    )


def _age_job(pw: PubWorld, job_id: str) -> None:
    """Move a terminal job's queue time 25 h back (the refresh window is long past)."""
    with pw.conn.transaction():
        pw.conn.execute("SET LOCAL session_replication_role = replica")
        pw.conn.execute(
            "UPDATE job_runs SET queued_at = queued_at - interval '25 hours' WHERE id = %s",
            [job_id],
        )


async def test_expired_key_reused_on_refresh_status_queues_a_new_job(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    key = "refresh-" + uuid.uuid4().hex
    url = f"/api/v1/listings/{made.listing_id}/refresh-status"
    first = await api.post(url, headers=pub.world.headers(OPERATOR, key))
    assert first.status_code == 202, first.text
    old = first.json()["job_id"]
    await run_job(uuid.UUID(old))
    assert pub.job(old)["status"] == "succeeded"
    _expire(pub, key)
    _age_job(pub, old)

    second = await api.post(url, headers=pub.world.headers(OPERATOR, key))
    assert second.status_code == 202, second.text
    body = second.json()
    assert body["job_id"] != old
    assert body["status"] == "queued"
    new = pub.job(body["job_id"])
    assert (new["status"], new["kind"]) == ("queued", "listing_status_sync")
    assert new["idempotency_key"] != pub.job(old)["idempotency_key"]
    assert pub.listing(made.listing_id)["last_event"] == "refresh_requested"

    replay = await api.post(url, headers=pub.world.headers(OPERATOR, key))
    assert (replay.status_code, replay.json()) == (202, body)


async def test_expired_key_reused_on_regenerate_is_accepted(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await generated(pub.world, OPERATOR)
    key = "regen-" + uuid.uuid4().hex
    url = f"/api/v1/listings/{made.listing_id}/regenerate"
    first = await api.post(url, headers=pub.world.headers(OPERATOR, key))
    assert first.status_code == 202, first.text
    old = first.json()["job_id"]
    await run_job(uuid.UUID(old))
    _expire(pub, key)

    second = await api.post(url, headers=pub.world.headers(OPERATOR, key))
    assert second.status_code == 202, second.text
    assert second.json()["job_id"] != old
    assert pub.job(second.json()["job_id"])["status"] == "queued"


async def test_expired_key_reused_on_publish_is_accepted(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    key = "pub-" + uuid.uuid4().hex
    body = [{"listing_id": str(made.listing_id)}]
    first = await post_publish(api, pub, OPERATOR, body, key=key)
    old = first.json()["items"][0]["job_id"]
    await run_job(uuid.UUID(old))
    # a new approved version, so only the job key could refuse a second publish
    set_status(pub.world, made.listing_id, "validated")
    new_version = await edited(api, pub.world, made, email=OWNER)
    approve = await post_approve(api, pub.world, REVIEWER, [item(new_version)])
    assert approve.json()["items"][0]["http_status"] == 200, approve.text
    _expire(pub, key)

    second = await post_publish(api, pub, OPERATOR, body, key=key)
    result = second.json()["items"][0]
    assert result["http_status"] == 202, result
    assert result["job_id"] != old


async def test_live_key_replays_and_duplicate_items_still_share_one_job(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """The scoping changes nothing inside one lifetime: a replay is the stored answer."""
    made = await approved(pub)
    key = "pub-" + uuid.uuid4().hex
    body = [{"listing_id": str(made.listing_id)}]
    first = await post_publish(api, pub, OPERATOR, body, key=key)
    again = await post_publish(api, pub, OPERATOR, body, key=key)
    assert again.json() == first.json()
    jobs = pub.scalar(
        "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
        [made.listing_id],
    )
    assert jobs == 1


async def test_refresh_status_never_resets_the_schedule_for_a_job_it_did_not_queue(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """A job key that already names a (finished) job is 409 LISTING_BUSY, and the listing keeps
    its sync slot. Called without a stored HTTP key, so the key derives as it did before."""
    from app.core.errors import ConflictError
    from app.repositories.uow import tenant_uow
    from app.services.publish import refresh_status
    from mkos_schemas.codes import ErrorCode

    made = await approved(pub)
    await publish_and_run(api, pub, made)
    principal = pub.world.principal(OPERATOR)
    key = "svc-" + uuid.uuid4().hex
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        old, _ = await refresh_status(uow, principal, made.listing_id, http_key=key)
    await run_job(old)
    _age_job(pub, str(old))
    pub.conn.execute(
        "UPDATE listings SET next_sync_at = now() + interval '1 hour', sync_poll_count = 3 "
        "WHERE id = %s",
        [made.listing_id],
    )
    with pytest.raises(ConflictError) as caught:
        async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
            await refresh_status(uow, principal, made.listing_id, http_key=key)
    assert caught.value.code is ErrorCode.LISTING_BUSY
    row = pub.listing(made.listing_id)
    assert row["next_sync_at"] is not None and row["sync_poll_count"] == 3
