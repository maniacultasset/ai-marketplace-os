"""SI11 T7 (AC1-AC3; P:A1-A2, D4-D6; CP-D5, CP-D16, CP-D24, CP-D26): the marketplace dry run as a
second body of `listing_status_sync`: its enqueue points, its verdict's writes, its hooks and the
account breaker."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem

from . import factories as f
from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs, preview_rows
from .publish_support import PubWorld, approved, pub, publish_and_run
from .review_support import edited, generated, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev", "pub"]


def _title_error() -> ErrorItem:
    return ErrorItem(
        field="content.title",
        error_code=ErrorCode.VALUE_NOT_ALLOWED,
        severity=Severity.ERROR,
        message="The title is longer than Amazon allows.",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code="99301",
    )


async def _run_previews(pw: PubWorld, listing_id: uuid.UUID) -> None:
    for job in preview_jobs(pw, listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])


def _check(pw: PubWorld, listing_id: uuid.UUID) -> tuple[Any, ...]:
    row = pw.listing(listing_id)
    return row["remote_check_version_id"], row["remote_check_status"], row["status"]


# ---- AC2: a generated listing is checked; ERROR blocks; an edit re-checks clean ----


async def test_generation_enqueues_one_preview_and_an_error_blocks(prev: PubWorld) -> None:
    prev.fake.preview_errors = [_title_error()]
    made = await generated(prev.world)
    (job,) = preview_jobs(prev, made.listing_id)
    assert job["created_by"] is None
    assert job["payload_json"]["version_id"] == str(made.version_id)
    assert "trigger" not in job["payload_json"]  # a system check, left out at its default
    assert _check(prev, made.listing_id) == (made.version_id, "pending", "validated")
    await run_job(job["id"])
    assert prev.job(job["id"])["status"] == "succeeded"
    (row,) = preview_rows(prev, made.listing_id)
    assert (row["source"], row["field_path"], row["status"]) == (
        "amazon_adapter",
        "content.title",
        "open",
    )
    assert row["listing_version_id"] == made.version_id
    listing = prev.listing(made.listing_id)
    assert (listing["status"], listing["status_reason"]) == ("blocked", "validation_errors")
    assert listing["remote_check_status"] == "issues" and listing["remote_check_at"] is not None
    assert prev.fake.preview_calls == 1


async def test_edit_rechecks_and_a_clean_verdict_unblocks(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    prev.fake.preview_errors = [_title_error()]
    made = await generated(prev.world)
    await _run_previews(prev, made.listing_id)
    prev.fake.preview_errors = []
    second = await edited(api, prev.world, made)
    jobs = preview_jobs(prev, made.listing_id)
    assert len(jobs) == 2 and jobs[1]["payload_json"]["version_id"] == str(second.version_id)
    await _run_previews(prev, made.listing_id)
    (row,) = preview_rows(prev, made.listing_id)
    assert row["status"] != "open"
    assert _check(prev, made.listing_id)[1] == "clean"
    assert prev.listing(made.listing_id)["status"] in ("validated", "needs_review")


async def test_replay_and_stale(api: httpx.AsyncClient, prev: PubWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.remote_preview import request_preview

    made = await generated(prev.world)
    await _run_previews(prev, made.listing_id)
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        assert await request_preview(uow, listing, made.version_id) is None  # replay
    assert len(preview_jobs(prev, made.listing_id)) == 1
    # a stale job: the version moves before the queued check runs
    prev.fake.preview_errors = [_title_error()]
    second = await edited(api, prev.world, made)
    queued = [j for j in preview_jobs(prev, made.listing_id) if j["status"] == "queued"]
    assert len(queued) == 1
    await edited(api, prev.world, second)  # a third version, while the second's check waits
    await run_job(queued[0]["id"])
    assert prev.job(queued[0]["id"])["result_json"] == {"skipped": "stale"}
    assert preview_rows(prev, made.listing_id) == []
    # the third version's pending check was enqueued by the stale job's terminal
    jobs = preview_jobs(prev, made.listing_id)
    assert len(jobs) == 3 and jobs[-1]["status"] == "queued"


async def test_failed_verdict_moves_nothing(prev: PubWorld) -> None:
    prev.fake.preview_status = "failed"
    made = await generated(prev.world)
    await _run_previews(prev, made.listing_id)
    assert _check(prev, made.listing_id) == (made.version_id, "failed", "validated")
    assert preview_rows(prev, made.listing_id) == []
    assert prev.listing(made.listing_id)["sync_poll_count"] == 0


async def test_dead_letter_marks_failed_and_counts_no_poll(prev: PubWorld) -> None:
    from app.adapters.amazon.client import failure
    from app.adapters.base import MarketplaceUnavailable
    from app.workers import previewers

    made = await generated(prev.world)
    (job,) = preview_jobs(prev, made.listing_id)

    async def broken(tenant_id: uuid.UUID, account_id: uuid.UUID) -> Any:
        raise MarketplaceUnavailable(failure(ErrorCode.MARKETPLACE_UNAVAILABLE, "down"))

    previewers.set_previewer_factory(broken)
    from app.workers.errors import RetryLater

    for _ in range(int(job["max_attempts"]) + 1):
        if prev.job(job["id"])["status"] in ("dead_letter", "failed"):
            break
        try:
            await run_job(job["id"])
        except RetryLater:
            continue
    assert prev.job(job["id"])["status"] in ("dead_letter", "failed")
    assert _check(prev, made.listing_id) == (made.version_id, "failed", "validated")
    assert prev.listing(made.listing_id)["sync_poll_count"] == 0


async def test_preview_after_approve_and_publish_is_stale(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    made = await approved(prev)
    (job,) = [j for j in preview_jobs(prev, made.listing_id) if j["status"] == "queued"]
    await publish_and_run(api, prev, made)
    prev.fake.preview_errors = [_title_error()]
    await run_job(job["id"])
    assert prev.job(job["id"])["result_json"]["skipped"] == "stale"
    assert preview_rows(prev, made.listing_id) == []
    assert prev.listing(made.listing_id)["status"] == "published"


async def test_approve_while_queued_then_error_voids_the_approval(prev: PubWorld) -> None:
    made = await approved(prev)
    prev.fake.preview_errors = [_title_error()]
    await _run_previews(prev, made.listing_id)
    listing = prev.listing(made.listing_id)
    assert (listing["status"], listing["approved_version_id"]) == ("blocked", None)


async def test_setting_off_and_flipkart_enqueue_nothing(pub: PubWorld) -> None:
    made = await generated(pub.world)
    assert preview_jobs(pub, made.listing_id) == []
    assert pub.listing(made.listing_id)["remote_check_status"] is None


async def test_no_active_account_enqueues_nothing(prev: PubWorld) -> None:
    prev.conn.execute(
        "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
        "kms_key_version = NULL, revoked_at = now() WHERE id = %s",
        [prev.accounts["acme"]],
    )
    made = await generated(prev.world)
    assert preview_jobs(prev, made.listing_id) == []


# ---- the account breaker (AC1, CP-D24) ----


async def test_breaker_stops_the_account_until_closed(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.remote_preview import request_preview

    prev.fake.preview_status, prev.fake.preview_applied = "failed", True
    first = await generated(prev.world)
    (job,) = preview_jobs(prev, first.listing_id)
    await run_job(job["id"])
    assert prev.job(job["id"])["result_json"]["preview_applied"] is True
    tasks = prev.rows(
        "SELECT id, kind, status FROM human_tasks WHERE marketplace_account_id = %s "
        "AND target_path = 'account.preview_applied'",
        [prev.accounts["acme"]],
    )
    assert [(t[1], t[2]) for t in tasks] == [("marketplace_error_review", "open")]
    prev.fake.preview_status, prev.fake.preview_applied = None, False
    second = await generated(prev.world)
    assert preview_jobs(prev, second.listing_id) == []
    # closing the task (SI9 `tasks_close`: answered -> applied) lifts it
    closed = await api.post(
        f"/api/v1/tasks/{tasks[0][0]}/close",
        headers=prev.world.headers("owner@acme.test", str(uuid.uuid4())),
        json={"resolution": "done"},
    )
    assert closed.status_code == 200, closed.text
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(second.listing_id)
        assert await request_preview(uow, listing, second.version_id) is not None


async def test_breaker_flag_without_task_refuses(prev: PubWorld) -> None:
    """The crash case: a flagged job and no task (the flag alone still refuses)."""
    made = await generated(prev.world)
    (job,) = preview_jobs(prev, made.listing_id)
    await run_job(job["id"])
    with prev.conn.transaction():  # set-up only: a terminal job row is immutable
        prev.conn.execute("SET LOCAL session_replication_role = replica")
        prev.conn.execute(
            "UPDATE job_runs SET result_json = result_json || "
            "'{\"preview_applied\": true}'::jsonb WHERE id = %s",
            [job["id"]],
        )
    other = await generated(prev.world)
    assert preview_jobs(prev, other.listing_id) == []


# ---- the live index (CP-D5) ----


async def test_live_sync_job_marks_pending(prev: PubWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.remote_preview import request_preview

    made = await generated(prev.world)
    (job,) = preview_jobs(prev, made.listing_id)
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        assert await request_preview(uow, listing, made.version_id, trigger="resolution") is None
    assert len(preview_jobs(prev, made.listing_id)) == 1
    assert prev.listing(made.listing_id)["remote_check_status"] == "pending"
    await run_job(job["id"])
    assert _check(prev, made.listing_id)[1] == "clean"


async def test_race_on_the_live_index_never_fails_the_caller(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.jobs import JobRunRepository
    from app.repositories.uow import tenant_uow
    from app.services.remote_preview import request_preview

    made = await generated(prev.world)
    preview_jobs(prev, made.listing_id)

    async def none(self: Any, listing_id: uuid.UUID, kind: Any) -> None:
        return None

    # a repair changes the repair set, so the key is new and the insert reaches the live index
    f.listing_repair(prev.conn, prev.world.tenant("acme"), made.listing_id)
    monkeypatch.setattr(JobRunRepository, "live_for_listing", none)
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        assert await request_preview(uow, listing, made.version_id, trigger="resolution") is None
        assert await uow.listings.get(made.listing_id) is not None  # still usable
    assert len(preview_jobs(prev, made.listing_id)) == 1
