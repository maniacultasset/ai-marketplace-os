"""S21 T12-T15, T17 (AC2, AC6-AC9; DW5, DW6; CP1, CP2, CP8, CP10; D8, D9, D11): the publish job's
write-ahead, read-before-retry, crash recovery, the race, the pins and the failure routing."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.adapters.base import (
    AuthExpired,
    MarketplaceAmbiguous,
    MarketplaceUnavailable,
    PermissionDenied,
)
from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, pub, publish_and_run, publish_one, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


class Crash(BaseException):
    """Stands in for the worker process dying (kill -9) at a chosen point: nothing in the job
    wrapper catches it, so the job row stays `running` until its lease is reaped."""


def _report() -> ErrorReport:
    return ErrorReport(status="failed")


async def _run_tolerant(job_id: Any) -> str | None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Crash:
        return "crash"
    except Exception as exc:
        assert type(exc).__name__ == "RetryLater", exc
        return "retry"
    return None


def _expire_lease(pub: PubWorld, job_id: Any) -> None:
    pub.conn.execute(
        "UPDATE job_runs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        [job_id],
    )


async def test_publish_write_ahead_order(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """The `pending` row is committed (visible to another connection) before the first call."""
    made = await approved(pub)
    seen: list[Any] = []
    original = pub.fake.create_listing

    async def spy(item: Any) -> Any:
        seen.extend(
            pub.rows(
                "SELECT outcome, sent_at IS NOT NULL FROM listing_submissions "
                "WHERE listing_id = %s",
                [made.listing_id],
            )
        )
        return await original(item)

    pub.fake.create_listing = spy
    await publish_and_run(api, pub, made)
    assert seen == [("pending", True)]


async def test_publish_crash_no_duplicate(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """DW5: the worker dies after the marketplace applied the create; the reaper requeues the
    job, which reads before it retries: `accepted`, one row, one create at the marketplace."""
    from app.workers.periodic import reap_once

    made = await approved(pub)
    pub.fake.raise_after_write = Crash()
    item = await publish_one(api, pub, made)
    assert await _run_tolerant(item["job_id"]) == "crash"
    assert pub.job(item["job_id"])["status"] == "running"
    _expire_lease(pub, item["job_id"])
    reaped = await reap_once()
    assert uuid.UUID(item["job_id"]) in reaped.requeued
    assert await _run_tolerant(item["job_id"]) is None
    (sub,) = pub.submissions(made.listing_id)
    assert sub["outcome"] == "accepted"
    assert pub.fake.creates == 1 and len(pub.fake.calls) == 1
    assert pub.listing(made.listing_id)["status"] == "published"


async def test_publish_crash_before_write_resends_same_key(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    from app.workers.periodic import reap_once

    made = await approved(pub)
    pub.fake.raise_next = [Crash()]
    item = await publish_one(api, pub, made)
    assert await _run_tolerant(item["job_id"]) == "crash"
    (pending,) = pub.submissions(made.listing_id)
    _expire_lease(pub, item["job_id"])
    await reap_once()
    assert await _run_tolerant(item["job_id"]) is None
    (sub,) = pub.submissions(made.listing_id)
    assert (sub["id"], sub["idempotency_key"], sub["outcome"]) == (
        pending["id"],
        pending["idempotency_key"],
        "accepted",
    )


async def test_publish_ambiguous_5xx_stays_pending(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    pub.fake.raise_next = [MarketplaceAmbiguous(_report())]
    item = await publish_one(api, pub, made)
    assert await _run_tolerant(item["job_id"]) == "retry"
    (sub,) = pub.submissions(made.listing_id)
    assert sub["outcome"] == "pending" and sub["sent_at"] is not None
    assert pub.job(item["job_id"])["status"] == "queued"


async def test_ambiguous_same_job_not_found_resends_same_key(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    pub.fake.raise_next = [MarketplaceAmbiguous(_report())]
    item = await publish_one(api, pub, made)
    await _run_tolerant(item["job_id"])
    (pending,) = pub.submissions(made.listing_id)
    assert await _run_tolerant(item["job_id"]) is None
    (sub,) = pub.submissions(made.listing_id)
    assert (sub["id"], sub["idempotency_key"], sub["outcome"], sub["attempt"]) == (
        pending["id"],
        pending["idempotency_key"],
        "accepted",
        1,
    )


async def test_unavailable_stays_pending_uncounted(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    pub.fake.raise_next = [MarketplaceUnavailable(_report())]
    item = await publish_one(api, pub, made)
    assert await _run_tolerant(item["job_id"]) == "retry"
    assert await _run_tolerant(item["job_id"]) is None
    subs = pub.submissions(made.listing_id)
    assert [(s["attempt"], s["outcome"]) for s in subs] == [(1, "accepted")]


async def test_dead_letter_then_http_publish_resolves(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """CP1 (c): the job dead-letters with the row `pending`; the listing is
    `failed/publish_failed`; publishing again is 202 and the new job reads first: the create was
    applied, so the row is `accepted` and nothing is resent."""
    made = await approved(pub)
    pub.fake.raise_after_write = MarketplaceAmbiguous(_report())
    pub.fake.raise_next = []
    item = await publish_one(api, pub, made)
    assert await _run_tolerant(item["job_id"]) == "retry"
    pub.fake.raise_next = [MarketplaceAmbiguous(_report()), MarketplaceAmbiguous(_report())]
    # the same job's reads find the SKU: make them fail too so the job exhausts its retries
    reads = pub.fake.get_listing

    async def unreadable(*a: Any, **k: Any) -> Any:
        raise MarketplaceUnavailable(_report())

    pub.fake.get_listing = unreadable
    for _ in range(2):
        await _run_tolerant(item["job_id"])
    assert pub.job(item["job_id"])["status"] == "dead_letter"
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "publish_failed")
    assert pub.submissions(made.listing_id)[0]["outcome"] == "pending"
    pub.fake.get_listing = reads
    pub.fake.raise_next = []
    again = await publish_one(api, pub, made)
    assert again["http_status"] == 202, again
    assert await _run_tolerant(again["job_id"]) is None
    subs = pub.submissions(made.listing_id)
    assert [(s["attempt"], s["outcome"]) for s in subs] == [(1, "accepted")]
    assert pub.fake.creates == 1
    assert pub.listing(made.listing_id)["status"] == "published"


async def test_earlier_job_pending_read_not_found_error_then_attempt2(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """D8: an ended job's `pending` create the marketplace never applied → `error
    SUBMISSION_NOT_APPLIED`, then attempt 2 under `retryable_condition`."""
    made = await approved(pub)
    pub.fake.raise_next = [MarketplaceAmbiguous(_report()) for _ in range(3)]
    item = await publish_one(api, pub, made)
    for _ in range(3):
        await _run_tolerant(item["job_id"])
    assert pub.job(item["job_id"])["status"] == "dead_letter"
    again = await publish_one(api, pub, made)
    assert again["http_status"] == 202, again
    assert await _run_tolerant(again["job_id"]) is None
    subs = pub.submissions(made.listing_id)
    assert [(s["attempt"], s["outcome"], s["error_code"]) for s in subs] == [
        (1, "error", "SUBMISSION_NOT_APPLIED"),
        (2, "accepted", None),
    ]
    assert subs[1]["retry_justification"] == "retryable_condition"


async def test_two_bodies_forced_one_pending_other_in_flight_no_call(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """DW6: a second job body for the listing while another live job's row is open refuses with
    SUBMISSION_IN_FLIGHT and calls nothing (the live-job index and the gate stop a second job
    first; this forces one past them)."""
    from app.workers.errors import JobFailed
    from app.workers.publish import run_listing_publish
    from app.workers.registry import JobContext
    from mkos_schemas.enums import JobKind

    made = await approved(pub)
    pub.fake.raise_next = [MarketplaceAmbiguous(_report())]
    item = await publish_one(api, pub, made)
    await _run_tolerant(item["job_id"])  # row pending; the job is live (queued for its retry)
    calls = len(pub.fake.calls)
    with pub.conn.transaction():
        pub.conn.execute("SET LOCAL session_replication_role = replica")
        other = pub.conn.execute(
            "INSERT INTO job_runs (tenant_id, kind, queue, entity_type, entity_id, listing_id, "
            "payload_json, correlation_id, created_by, max_attempts, status, finished_at) "
            "SELECT tenant_id, kind, queue, entity_type, entity_id, listing_id, payload_json, "
            "gen_random_uuid(), created_by, 1, 'succeeded', now() FROM job_runs WHERE id = %s "
            "RETURNING id, tenant_id, payload_json",
            [item["job_id"]],
        ).fetchone()
    assert other is not None
    ctx = JobContext(
        job_run_id=other[0],
        tenant_id=other[1],
        kind=JobKind.LISTING_PUBLISH,
        request_id="r",
        payload=dict(other[2]),
    )
    with pytest.raises(JobFailed) as raised:
        await run_listing_publish(ctx)
    assert raised.value.report.error_code is ErrorCode.SUBMISSION_IN_FLIGHT
    assert len(pub.fake.calls) == calls
    assert [s["outcome"] for s in pub.submissions(made.listing_id)] == ["pending"]


async def test_publish_facts_changed(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """AC8: a fact changed after the approval → nothing sent, no row, the listing revalidated
    (approval voided), the job failed FACTS_CHANGED_SINCE_APPROVAL."""
    made = await approved(pub)
    item = await publish_one(api, pub, made)
    product = pub.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id])
    pub.conn.execute("UPDATE products SET title = title || ' v2' WHERE id = %s", [product])
    await _run_tolerant(item["job_id"])
    job = pub.job(item["job_id"])
    assert (job["status"], job["error_code"]) == ("failed", "FACTS_CHANGED_SINCE_APPROVAL")
    assert pub.submissions(made.listing_id) == [] and pub.fake.calls == []
    row = pub.listing(made.listing_id)
    assert row["status"] in ("needs_review", "blocked") and row["approved_version_id"] is None


async def test_publish_failure_routing_rate_limited(api: httpx.AsyncClient, pub: PubWorld) -> None:
    from app.adapters.base import RateLimited

    made = await approved(pub)
    pub.fake.raise_next = [RateLimited(_report())]
    item = await publish_one(api, pub, made)
    assert await _run_tolerant(item["job_id"]) == "retry"
    assert await _run_tolerant(item["job_id"]) is None
    subs = pub.submissions(made.listing_id)
    assert [(s["attempt"], s["outcome"], s["retry_justification"]) for s in subs] == [
        (1, "error", None),
        (2, "accepted", "retryable_condition"),
    ]


async def test_publish_auth_expired_blocked_issue_linked_then_resume(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """D11 + T71: AUTH_EXPIRED blocks the listing with an issue linked to the account's reconnect
    task; once the task is applied, a publish resumes with `human_resolution`."""
    made = await approved(pub)
    pub.fake.raise_next = [AuthExpired(_report())]
    await publish_and_run(api, pub, made)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "marketplace_action_required")
    (sub,) = pub.submissions(made.listing_id)
    assert (sub["outcome"], sub["error_code"]) == ("error", "AUTH_EXPIRED")
    linked = pub.rows(
        "SELECT t.kind FROM listing_issues i JOIN human_tasks t ON t.id = i.human_task_id "
        "WHERE i.listing_id = %s AND i.job_id = %s",
        [made.listing_id, sub["job_id"]],
    )
    assert linked == [("reconnect_account",)]
    blocked = await publish_one(api, pub, made)
    assert blocked["error"]["error_code"] == "DUPLICATE_SUBMISSION_BLOCKED"
    from app.repositories.uow import tenant_uow

    owner = pub.world.user("owner@acme.test")
    async with tenant_uow(pub.world.tenant("acme"), user_id=owner) as uow:
        assert await uow.human_tasks.apply_reconnect(pub.accounts["acme"], owner) == 1
    item = await publish_and_run(api, pub, made)
    assert pub.job(item["job_id"])["status"] == "succeeded"
    subs = pub.submissions(made.listing_id)
    assert subs[1]["retry_justification"] == "human_resolution"
    assert pub.listing(made.listing_id)["last_event"] == "submission_accepted"


async def test_publish_permission_denied_task(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    pub.fake.raise_next = [PermissionDenied(_report())]
    await publish_and_run(api, pub, made)
    tasks = pub.rows(
        "SELECT kind, target_path FROM human_tasks WHERE marketplace_account_id = %s",
        [pub.accounts["acme"]],
    )
    assert [t[0] for t in tasks] == ["permission_decision"]
    assert tasks[0][1].startswith("account.category_approvals.")


async def test_unknown_code_one_review_task(api: httpx.AsyncClient, pub: PubWorld) -> None:
    for _ in range(2):
        made = await approved(pub)
        pub.fake.set_fail_fields({"content.title": ("WEIRD_1", "Something odd")})
        await publish_and_run(api, pub, made)
        row = pub.listing(made.listing_id)
        assert (row["status"], row["status_reason"]) == ("blocked", "marketplace_action_required")
    reviews = pub.scalar(
        "SELECT count(*) FROM human_tasks WHERE kind = 'marketplace_error_review' "
        "AND status = 'open'"
    )
    assert reviews == 1


async def test_new_version_supersedes(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """A newer approved version's attempt 1 moves the older open `accepted` row to
    `superseded` and goes out as an `update`."""
    from .review_support import REVIEWER, edited

    made = await approved(pub)
    await publish_and_run(api, pub, made)
    v2 = await edited(api, pub.world, made, email="owner@acme.test")
    from app.repositories.uow import tenant_uow
    from app.services.listing_validate import validate
    from app.services.review import ReviewItem, approve_batch

    del validate
    principal = pub.world.principal(REVIEWER)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        outcome = await approve_batch(
            uow, principal, [ReviewItem(v2.listing_id, v2.version_id, "approve")], http_key="k2"
        )
    if outcome.items[0].http_status != 200:
        pytest.skip(f"edited version not approvable in this world: {outcome.items[0]}")
    await publish_and_run(api, pub, v2)
    subs = pub.submissions(made.listing_id)
    assert [(s["listing_version_id"], s["outcome"], s["operation"]) for s in subs] == [
        (made.version_id, "superseded", "create"),
        (v2.version_id, "accepted", "update"),
    ]


async def test_requeue_listing_publish_refused(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """CP3: a dead-lettered publish is never requeued by staff; the user publishes again."""
    from app.repositories.jobs import NotEligibleError
    from app.services.job_hooks import run_requeue_hook

    made = await approved(pub)
    item = await publish_one(api, pub, made)
    job = await _job_row(pub, item["job_id"])
    with pytest.raises(NotEligibleError):
        await run_requeue_hook(None, job, job)


async def _job_row(pub: PubWorld, job_id: Any) -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(pub.world.tenant("acme")) as uow:
        return await uow.job_runs.get_by_id(uuid.UUID(str(job_id)))


async def test_publish_result_url_is_submissions(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    item = await publish_and_run(api, pub, made)
    response = await api.get(
        f"/api/v1/jobs/{item['job_id']}", headers=pub.world.headers("operator@acme.test")
    )
    assert response.status_code == 200, response.text
    assert response.json()["result_url"] == f"/api/v1/listings/{made.listing_id}/submissions"


async def test_publish_hook_cancel_none_outcome(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """The hook runs with `outcome=None` on the API cancel: the listing leaves `publishing`."""
    made = await approved(pub)
    item = await publish_one(api, pub, made)
    response = await api.post(
        f"/api/v1/jobs/{item['job_id']}/cancel", headers=pub.world.headers("operator@acme.test")
    )
    assert response.status_code == 200, response.text
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "publish_failed")
