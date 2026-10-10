"""S21 T6, T7 (AC2-AC7; DATABASE.md :834-887): the submission ledger, field-result and item
repositories against real publish attempts (as `app_rw`, RLS forced)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, pub, publish_and_run, retry, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]
RL = ("Throttled", "Request was throttled", ErrorCode.RATE_LIMITED)


def _uow(pub: PubWorld, slug: str = "acme", email: str | None = None) -> Any:
    from app.repositories.uow import tenant_uow

    user = None if email is None else pub.world.user(email)
    return tenant_uow(pub.world.tenant(slug), user_id=user)


async def test_retry_payload_latest_then_failed(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """A field that failed and later succeeded is never resent; one that failed twice once."""
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL, "content.description": RL})
    await publish_and_run(api, pub, made)
    pub.fake.set_fail_fields({"content.description": RL})
    response = await retry(api, pub, made)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    async with _uow(pub) as uow:
        payload = await uow.field_results.retry_payload(made.listing_id, made.version_id)
    assert list(payload.values()) == [("content.description",)]
    pub.fake.set_fail_fields({})
    response = await retry(api, pub, made)
    await run_job(uuid.UUID(response.json()["job_id"]))
    subs = pub.submissions(made.listing_id)
    assert [(s["attempt"], s["field_paths"], s["outcome"]) for s in subs] == [
        (1, None, "partially_accepted"),
        (2, ["content.description", "content.title"], "partially_accepted"),
        (3, ["content.description"], "accepted"),
    ]
    assert pub.fake.calls[-1][1] == {next(iter(payload)): ["content.description"]}


async def test_retry_payload_other_version_ignored(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    async with _uow(pub) as uow:
        assert await uow.field_results.retry_payload(made.listing_id, uuid.uuid4()) == {}
        assert await uow.field_results.retry_payload(made.listing_id, made.version_id)


async def test_resolve_fenced_other_job_0_rows(api: httpx.AsyncClient, pub: PubWorld) -> None:
    from app.adapters.base import MarketplaceAmbiguous
    from mkos_schemas.errors import ErrorReport

    made = await approved(pub)
    pub.fake.raise_after_write = MarketplaceAmbiguous(ErrorReport(status="failed"))
    item = await _publish_raising(api, pub, made)
    (sub,) = pub.submissions(made.listing_id)
    assert sub["outcome"] == "pending" and sub["sent_at"] is not None
    async with _uow(pub) as uow:
        stale = await uow.submissions.resolve(sub["id"], outcome="accepted", job_id=uuid.uuid4())
    assert stale is False
    assert pub.submissions(made.listing_id)[0]["outcome"] == "pending"
    assert item["http_status"] == 202


async def _publish_raising(api: httpx.AsyncClient, pub: PubWorld, made: Any) -> dict[str, Any]:
    from .publish_support import publish_one

    item = await publish_one(api, pub, made)
    try:
        await run_job(uuid.UUID(item["job_id"]))
    except Exception as exc:  # RetryLater: the wrapper schedules the transport retry
        assert type(exc).__name__ == "RetryLater"
    return item


async def test_second_open_refused_in_flight(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """`ux_ls_open` (the race backstop under `ls_guard`): a second open row of a listing is a
    unique violation on that index, which the API maps to 409 SUBMISSION_IN_FLIGHT."""
    import psycopg
    from app.adapters.base import MarketplaceAmbiguous
    from app.db.integrity import CONSTRAINT_CODES
    from mkos_schemas.errors import ErrorReport

    made = await approved(pub)
    pub.fake.raise_after_write = MarketplaceAmbiguous(ErrorReport(status="failed"))
    await _publish_raising(api, pub, made)
    (sub,) = pub.submissions(made.listing_id)
    assert CONSTRAINT_CODES["ux_ls_open"] is ErrorCode.SUBMISSION_IN_FLIGHT
    with pytest.raises(psycopg.errors.UniqueViolation) as raised, pub.conn.transaction():
        pub.conn.execute("SET LOCAL session_replication_role = replica")  # past ls_guard
        pub.conn.execute(
            "INSERT INTO listing_submissions (tenant_id, listing_id, listing_version_id, "
            "marketplace, marketplace_account_id, job_id, attempt, operation, payload_sha256, "
            "payload_storage_key, facts_digest, idempotency_key, requested_by_user_id, "
            "actor_type, retry_justification, previous_submission_id) SELECT tenant_id, "
            "listing_id, listing_version_id, marketplace, marketplace_account_id, job_id, 2, "
            "operation, payload_sha256, payload_storage_key, facts_digest, 'other-key', "
            "requested_by_user_id, actor_type, 'retryable_condition', id "
            "FROM listing_submissions WHERE id = %s",
            [sub["id"]],
        )
    assert raised.value.diag.constraint_name == "ux_ls_open"


async def test_page_newest_first_keyset_and_other_tenant_absent(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    pub.fake.set_fail_fields({})
    response = await retry(api, pub, made)
    await run_job(uuid.UUID(response.json()["job_id"]))
    async with _uow(pub) as uow:
        first, more = await uow.submissions.page(made.listing_id, after=None, limit=1)
        assert more and first[0].attempt == 2
        second, more = await uow.submissions.page(
            made.listing_id, after=(first[0].created_at, first[0].id), limit=1
        )
        assert not more and second[0].attempt == 1
        assert await uow.submissions.count_for_version(made.listing_id, made.version_id) == 2
    async with _uow(pub, "bharat") as uow:
        rows, _ = await uow.submissions.page(made.listing_id, after=None, limit=50)
        assert rows == []
        assert await uow.field_results.retry_payload(made.listing_id, made.version_id) == {}


async def test_items_upsert_idempotent_and_sku_immutable(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    import psycopg
    from app.repositories.submissions import NewItem

    made = await approved(pub)
    await publish_and_run(api, pub, made)
    items = pub.rows(
        "SELECT id, seller_sku, item_role, first_submitted_at FROM listing_items "
        "WHERE listing_id = %s",
        [made.listing_id],
    )
    assert len(items) == 1 and items[0][2] == "standalone" and items[0][3] is not None
    product = pub.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id])
    async with _uow(pub) as uow:
        rows = await uow.listing_items.upsert_for_submission(
            made.listing_id, "amazon", [NewItem(product, "standalone", items[0][1])]
        )
    assert [r.id for r in rows.values()] == [items[0][0]]
    with pytest.raises(psycopg.Error) as raised:
        pub.conn.execute(
            "UPDATE listing_items SET seller_sku = 'RENAMED' WHERE id = %s", [items[0][0]]
        )
    assert raised.value.sqlstate == "LIT01"


async def test_set_sync_does_not_stamp_last_event(api: httpx.AsyncClient, pub: PubWorld) -> None:
    from app.repositories.listings import DbNow

    made = await approved(pub)
    await publish_and_run(api, pub, made)
    before = pub.listing(made.listing_id)
    async with _uow(pub) as uow:
        await uow.listings.set_columns(made.listing_id, next_sync_at=DbNow(), sync_poll_count=1)
    after = pub.listing(made.listing_id)
    assert (after["last_event"], after["last_event_at"]) == (
        before["last_event"],
        before["last_event_at"],
    )
    assert after["sync_poll_count"] == 1
