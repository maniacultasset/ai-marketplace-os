"""app/services/publish.py without a database (S26 T23 gate-fix; AC 11): the per-item refusal
report, the gate, the CP9 justification inputs, the plan, the enqueue, the field retry and the
refresh-status request, each over a stub unit of work whose calls are recorded. The integration
suites (test_listings_publish_api.py, test_publish_flow.py, test_listings_sync_reads_api.py)
prove the same paths against PostgreSQL; these pin the exact arguments and answers."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core.errors import ConflictError, ForbiddenError, LimitError, NotFoundError
from app.core.rbac import Principal
from app.core.request_context import request_context
from app.repositories.jobs import EnqueueSpec
from app.services import publish as svc
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
VERSION = uuid.UUID(int=7)
JOB = uuid.UUID(int=99)


def _principal() -> Principal:
    return Principal(
        user_id=uuid.UUID(int=1),
        tenant_id=uuid.UUID(int=2),
        role="operator",
        email="operator@acme.test",
        jti="jti-1",
    )


def _listing(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.UUID(int=10),
        "status": "approved",
        "status_reason": None,
        "paused_at": None,
        "archived_at": None,
        "marketplace": "amazon",
        "marketplace_account_id": uuid.UUID(int=11),
        "current_version_id": VERSION,
        "approved_version_id": VERSION,
        "marketplace_state_changed_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _job(job_id: uuid.UUID = JOB, status: str = "queued") -> SimpleNamespace:
    return SimpleNamespace(id=job_id, status=status)


def _uow(**over: Any) -> Any:
    """A unit of work whose repositories answer the happy publish path by default."""
    uow = SimpleNamespace(
        marketplace_accounts=SimpleNamespace(active_for_publish=AsyncMock(return_value="acct")),
        submissions=SimpleNamespace(
            latest=AsyncMock(return_value=None),
            count_for_version=AsyncMock(return_value=0),
            last_for_version=AsyncMock(return_value=None),
        ),
        job_runs=SimpleNamespace(
            get_by_id=AsyncMock(return_value=None),
            enqueue=AsyncMock(return_value=SimpleNamespace(job=_job(), created=True)),
            live_for_listing=AsyncMock(return_value=None),
            user_requested_since=AsyncMock(return_value=False),
        ),
        listings=SimpleNamespace(
            latest_job_id=AsyncMock(return_value=uuid.UUID(int=55)),
            move=AsyncMock(),
            get_for_update=AsyncMock(return_value=None),
            set_columns=AsyncMock(),
        ),
        listing_issues=SimpleNamespace(task_ids_for_job=AsyncMock(return_value=["t1"])),
        human_tasks=SimpleNamespace(any_applied_after=AsyncMock(return_value=False)),
        field_results=SimpleNamespace(retry_payload=AsyncMock(return_value={"title": "x"})),
        audit=SimpleNamespace(write=AsyncMock()),
    )
    for path, value in over.items():
        repo, attr = path.split("__")
        setattr(getattr(uow, repo), attr, value)
    return uow


def _spec(uow: SimpleNamespace) -> EnqueueSpec:
    spec = uow.job_runs.enqueue.await_args.args[0]
    assert isinstance(spec, EnqueueSpec)
    return spec


# ---- item_report and Refused ----


def test_item_report_is_the_full_errorreport() -> None:
    listing_id = uuid.uuid4()
    with request_context("req-abc"):
        report = svc.item_report(ErrorCode.LISTING_PAUSED, listing_id)
    assert report.model_dump(mode="json") == {
        "schema_version": report.schema_version,
        "status": "failed",
        "error_code": "LISTING_PAUSED",
        "retryable": False,
        "attempts_used": report.attempts_used,
        "max_attempts": report.max_attempts,
        "errors": [
            {
                **report.errors[0].model_dump(mode="json"),
                "field": "listing",
                "error_code": "LISTING_PAUSED",
                "message": "The listing is paused.",
                "source": ErrorSource.SYSTEM.value,
                "retryable": False,
            }
        ],
        "request_id": "req-abc",
        "listing_id": str(listing_id),
    }


def test_item_report_auto_retry_code_is_retryable_with_the_default_message() -> None:
    report = svc.item_report(ErrorCode.RATE_LIMITED)
    assert report.retryable is True
    assert report.errors[0].retryable is True
    assert report.errors[0].message == "The item was refused."
    assert report.request_id is None
    assert report.listing_id is None


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorCode.NOT_FOUND, 404),
        (ErrorCode.NOT_IMPLEMENTED, 501),
        (ErrorCode.INTERNAL_ERROR, 500),
        (ErrorCode.LISTING_BUSY, 409),
    ],
)
def test_refused_status(code: ErrorCode, status: int) -> None:
    refused = svc.Refused(code)
    assert (refused.code, refused.status, str(refused)) == (code, status, code.value)


# ---- the gate ----


@pytest.mark.parametrize(
    ("listing", "version", "code"),
    [
        (None, None, ErrorCode.NOT_FOUND),
        (_listing(paused_at=T0), None, ErrorCode.LISTING_PAUSED),
        (_listing(archived_at=T0), None, ErrorCode.LISTING_ARCHIVED),
        (_listing(status="generating"), None, ErrorCode.LISTING_BUSY),
        (_listing(status="publishing"), None, ErrorCode.LISTING_BUSY),
        (_listing(), uuid.UUID(int=8), ErrorCode.STALE_VERSION),
        (_listing(marketplace="flipkart"), None, ErrorCode.NOT_IMPLEMENTED),
        (_listing(current_version_id=None, approved_version_id=None), None, None),
        (_listing(approved_version_id=uuid.UUID(int=8)), None, None),
    ],
)
def test_gate_refusals(listing: Any, version: uuid.UUID | None, code: ErrorCode | None) -> None:
    with pytest.raises(svc.Refused) as refused:
        svc._gate(listing, version)
    assert refused.value.code is (code or ErrorCode.INVALID_STATUS_TRANSITION)


def test_gate_passes_the_current_approved_version() -> None:
    listing = _listing()
    assert svc._gate(listing, VERSION) is listing
    assert svc._gate(listing, None) is listing


# ---- account, unresolved pending, justification ----


async def test_account_asks_for_the_listing_account_and_refuses_none() -> None:
    uow = _uow()
    listing = _listing()
    assert await svc._account(uow, listing) == "acct"
    uow.marketplace_accounts.active_for_publish.assert_awaited_once_with(
        "amazon", listing.marketplace_account_id
    )
    uow.marketplace_accounts.active_for_publish = AsyncMock(return_value=None)
    with pytest.raises(svc.Refused) as refused:
        await svc._account(uow, listing)
    assert refused.value.code is ErrorCode.AUTH_EXPIRED


async def test_unresolved_pending() -> None:
    listing_id = uuid.uuid4()
    pending = SimpleNamespace(outcome="pending", job_id=uuid.UUID(int=5))
    uow = _uow(submissions__latest=AsyncMock(return_value=None))
    assert await svc.unresolved_pending(uow, listing_id) is None
    uow.submissions.latest.assert_awaited_once_with(listing_id)
    uow.submissions.latest = AsyncMock(return_value=SimpleNamespace(outcome="error"))
    assert await svc.unresolved_pending(uow, listing_id) is None
    uow.submissions.latest = AsyncMock(return_value=pending)
    for status in sorted(svc.TERMINAL_JOB):
        uow.job_runs.get_by_id = AsyncMock(return_value=_job(status=status))
        assert await svc.unresolved_pending(uow, listing_id) is pending
        uow.job_runs.get_by_id.assert_awaited_once_with(pending.job_id)
    uow.job_runs.get_by_id = AsyncMock(return_value=None)
    assert await svc.unresolved_pending(uow, listing_id) is pending
    uow.job_runs.get_by_id = AsyncMock(return_value=_job(status="running"))
    with pytest.raises(svc.Refused) as refused:
        await svc.unresolved_pending(uow, listing_id)
    assert refused.value.code is ErrorCode.LISTING_BUSY


def _spy_choose(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    calls: list[tuple[Any, dict[str, Any]]] = []

    def spy(prev: Any, **kwargs: Any) -> str:
        calls.append((prev, kwargs))
        return "picked"

    monkeypatch.setattr(svc, "choose_justification", spy)
    return calls


async def test_justification_unresolved_attempt_reads_no_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy_choose(monkeypatch)
    uow = _uow()
    prev = SimpleNamespace(outcome="error", error_report_json={"a": 1}, resolved_at=None, job_id=1)
    listing = _listing(marketplace_state_changed_at=T0)
    assert await svc.justification_for(uow, listing, prev) == "picked"
    uow.listing_issues.task_ids_for_job.assert_not_awaited()
    attempt, kwargs = calls[0]
    assert (attempt.outcome, attempt.error_report, attempt.resolved_at) == ("error", {"a": 1}, None)
    assert kwargs == {"applied_task_after": False, "marketplace_changed_after": False}


@pytest.mark.parametrize(
    ("changed_at", "applied", "moved"),
    [
        (None, False, False),
        (T0 - timedelta(seconds=1), True, False),
        (T0, False, False),
        (T0 + timedelta(seconds=1), True, True),
    ],
)
async def test_justification_resolved_attempt(
    monkeypatch: pytest.MonkeyPatch, changed_at: datetime | None, applied: bool, moved: bool
) -> None:
    calls = _spy_choose(monkeypatch)
    uow = _uow(human_tasks__any_applied_after=AsyncMock(return_value=applied))
    prev = SimpleNamespace(outcome="rejected", error_report_json=None, resolved_at=T0, job_id=42)
    listing = _listing(marketplace_state_changed_at=changed_at)
    await svc.justification_for(uow, listing, prev)
    uow.listing_issues.task_ids_for_job.assert_awaited_once_with(42)
    uow.human_tasks.any_applied_after.assert_awaited_once_with(["t1"], T0)
    assert calls[0][1] == {"applied_task_after": applied, "marketplace_changed_after": moved}


# ---- plan_publish ----


async def test_plan_first_publish() -> None:
    uow = _uow()
    plan = await svc.plan_publish(uow, _listing())
    assert plan == svc.Plan(event="publish", mode="publish", parent_job_id=None)
    uow.listings.latest_job_id.assert_not_awaited()
    uow.submissions.count_for_version.assert_awaited_once_with(uuid.UUID(int=10), VERSION)


async def test_plan_retry_names_the_parent_job() -> None:
    uow = _uow()
    listing = _listing(status="failed", status_reason="publish_failed")
    plan = await svc.plan_publish(uow, listing)
    assert plan == svc.Plan(event="retry_publish", mode="publish", parent_job_id=uuid.UUID(int=55))
    uow.listings.latest_job_id.assert_awaited_once_with(listing.id, JobKind.LISTING_PUBLISH)


async def test_plan_exhausted_and_no_event() -> None:
    uow = _uow()
    blocked = _listing(status="blocked", status_reason="publish_attempts_exhausted")
    with pytest.raises(svc.Refused) as refused:
        await svc.plan_publish(uow, blocked)
    assert refused.value.code is ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED
    for listing in (_listing(status="draft"), _listing(status="blocked", status_reason="x")):
        with pytest.raises(svc.Refused) as refused:
            await svc.plan_publish(uow, listing)
        assert refused.value.code is ErrorCode.INVALID_STATUS_TRANSITION
    uow.marketplace_accounts.active_for_publish.assert_not_awaited()


async def test_plan_retry_fields_only_needs_a_failed_publish() -> None:
    uow = _uow()
    for listing in (
        _listing(),
        _listing(status="failed", status_reason="publish_attempts_exhausted"),
    ):
        with pytest.raises(svc.Refused) as refused:
            await svc.plan_publish(uow, listing, retry_fields_only=True)
        assert refused.value.code is ErrorCode.INVALID_STATUS_TRANSITION
    # retry_fields on a version never sent is a plain publish
    listing = _listing(status="failed", status_reason="publish_partial")
    plan = await svc.plan_publish(uow, listing, retry_fields_only=True)
    assert plan == svc.Plan(event="retry_publish", mode="publish", parent_job_id=uuid.UUID(int=55))


async def test_plan_resolve_then_retry_skips_the_cap() -> None:
    pending = SimpleNamespace(outcome="pending", job_id=uuid.UUID(int=5))
    uow = _uow(
        submissions__latest=AsyncMock(return_value=pending),
        job_runs__get_by_id=AsyncMock(return_value=_job(status="failed")),
        submissions__count_for_version=AsyncMock(return_value=3),
    )
    listing = _listing(status="failed", status_reason="publish_partial")
    plan = await svc.plan_publish(uow, listing)
    assert plan == svc.Plan(event="retry_publish", mode="publish", parent_job_id=uuid.UUID(int=55))
    uow.submissions.count_for_version.assert_not_awaited()


@pytest.mark.parametrize("sent", [3, 4])
async def test_plan_cap(sent: int) -> None:
    uow = _uow(submissions__count_for_version=AsyncMock(return_value=sent))
    with pytest.raises(svc.Refused) as refused:
        await svc.plan_publish(uow, _listing(status="failed", status_reason="publish_failed"))
    assert refused.value.code is ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED


def _prev(outcome: str, resolved_at: datetime | None = T0) -> SimpleNamespace:
    return SimpleNamespace(
        outcome=outcome,
        error_report_json={"retryable": True, "errors": [{"retryable": True}]},
        resolved_at=resolved_at,
        job_id=1,
    )


async def test_plan_second_attempt_justified() -> None:
    uow = _uow(
        submissions__count_for_version=AsyncMock(return_value=2),
        submissions__last_for_version=AsyncMock(return_value=_prev("error")),
    )
    listing = _listing(status="failed", status_reason="publish_failed")
    plan = await svc.plan_publish(uow, listing)
    assert plan == svc.Plan(event="retry_publish", mode="publish", parent_job_id=uuid.UUID(int=55))
    uow.submissions.last_for_version.assert_awaited_once_with(listing.id, VERSION)


async def test_plan_partial_retry_fields_mode_and_payload() -> None:
    uow = _uow(
        submissions__count_for_version=AsyncMock(return_value=1),
        submissions__last_for_version=AsyncMock(return_value=_prev("partially_accepted")),
    )
    listing = _listing(status="failed", status_reason="publish_partial")
    plan = await svc.plan_publish(uow, listing, retry_fields_only=True)
    assert plan.mode == "retry_fields"
    uow.field_results.retry_payload.assert_awaited_once_with(listing.id, VERSION)
    uow.field_results.retry_payload = AsyncMock(return_value={})
    with pytest.raises(svc.Refused) as refused:
        await svc.plan_publish(uow, listing, retry_fields_only=True)
    assert refused.value.code is ErrorCode.INVALID_STATUS_TRANSITION


async def test_plan_retry_fields_after_a_non_partial_attempt_is_a_full_publish() -> None:
    uow = _uow(
        submissions__count_for_version=AsyncMock(return_value=1),
        submissions__last_for_version=AsyncMock(return_value=_prev("error")),
    )
    listing = _listing(status="failed", status_reason="publish_partial")
    plan = await svc.plan_publish(uow, listing, retry_fields_only=True)
    assert plan.mode == "publish"
    uow.field_results.retry_payload.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["accepted", "live", "superseded", "pending"])
async def test_plan_refuses_after_a_sent_attempt(outcome: str) -> None:
    uow = _uow(
        submissions__count_for_version=AsyncMock(return_value=1),
        submissions__last_for_version=AsyncMock(return_value=_prev(outcome)),
    )
    with pytest.raises(svc.Refused) as refused:
        await svc.plan_publish(uow, _listing(status="failed", status_reason="publish_failed"))
    assert refused.value.code is ErrorCode.INVALID_STATUS_TRANSITION


async def test_plan_unjustified_retry_and_missing_last() -> None:
    uow = _uow(
        submissions__count_for_version=AsyncMock(return_value=1),
        submissions__last_for_version=AsyncMock(return_value=_prev("error", resolved_at=None)),
    )
    listing = _listing(status="failed", status_reason="publish_failed")
    with pytest.raises(svc.Refused) as refused:
        await svc.plan_publish(uow, listing)
    assert refused.value.code is ErrorCode.DUPLICATE_SUBMISSION_BLOCKED
    uow.submissions.last_for_version = AsyncMock(return_value=None)
    with pytest.raises(svc.Refused) as refused:
        await svc.plan_publish(uow, listing)
    assert refused.value.code is ErrorCode.INVALID_STATUS_TRANSITION


# ---- gate refusal mapping, enqueue ----


def test_gate_refusal_mapping() -> None:
    assert svc._gate_refusal(NotFoundError("x")) is ErrorCode.NOT_FOUND
    assert (
        svc._gate_refusal(ConflictError(ErrorCode.INTERNAL_ERROR, "c"))
        is ErrorCode.INVALID_STATUS_TRANSITION
    )
    assert svc._gate_refusal(ConflictError(ErrorCode.LISTING_BUSY, "c")) is ErrorCode.LISTING_BUSY
    assert svc._gate_refusal(ForbiddenError("x")) is ErrorCode.INTERNAL_ERROR


async def test_enqueue_publish_writes_job_move_and_audit() -> None:
    uow = _uow()
    principal = _principal()
    listing = _listing(status="failed", status_reason="publish_failed")
    plan = svc.Plan(event="retry_publish", mode="publish", parent_job_id=uuid.UUID(int=55))
    batch = uuid.UUID(int=77)
    job_id = await svc.enqueue_publish(
        uow,
        principal,
        listing,
        plan,
        http_key="key-1",
        batch_id=batch,
    )
    assert job_id == uuid.UUID(int=99)
    assert _spec(uow) == EnqueueSpec(
        kind=JobKind.LISTING_PUBLISH,
        entity_type="listing",
        entity_id=listing.id,
        listing_id=listing.id,
        created_by=principal.user_id,
        http_key="key-1",
        payload={
            "v": 1,
            "listing_id": str(listing.id),
            "listing_version_id": str(VERSION),
            "mode": "publish",
        },
        batch_id=batch,
        parent_job_id=uuid.UUID(int=55),
    )
    move = uow.listings.move.await_args
    assert move.args == (listing,)
    assert move.kwargs["to"] == "publishing"
    assert move.kwargs["event"] == "retry_publish"
    assert move.kwargs["actor"].user_id == principal.user_id
    uow.audit.write.assert_awaited_once_with(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action="listing.publish_requested",
        entity_type="listing",
        entity_id=listing.id,
        before=json.dumps({"status": "failed"}),
        after=json.dumps({"status": "publishing"}),
        reason="retry_publish",
    )


async def test_enqueue_publish_falls_back_to_current_and_refuses_a_live_job() -> None:
    uow = _uow()
    uow.job_runs.enqueue = AsyncMock(return_value=SimpleNamespace(job=_job(), created=False))
    listing = _listing(approved_version_id=None)
    plan = svc.Plan(event="publish", mode="publish", parent_job_id=None)
    with pytest.raises(svc.Refused) as refused:
        await svc.enqueue_publish(uow, _principal(), listing, plan, http_key=None)
    assert refused.value.code is ErrorCode.LISTING_BUSY
    assert _spec(uow).payload == {
        "v": 1,
        "listing_id": str(listing.id),
        "listing_version_id": str(VERSION),
        "mode": "publish",
    }
    assert _spec(uow).batch_id is None
    uow.listings.move.assert_not_awaited()


# ---- batch body ----


def test_batch_body() -> None:
    batch = uuid.UUID(int=3)
    report = svc.item_report(ErrorCode.LISTING_BUSY)
    refused = svc.ItemOutcome(uuid.UUID(int=4), 409, "generating", error=report)
    accepted = svc.ItemOutcome(uuid.UUID(int=5), 202, "publishing", uuid.UUID(int=6))
    body = svc.batch_body(batch, [refused, accepted])
    assert body == {
        "batch_id": str(batch),
        "items": [
            {
                "listing_id": str(uuid.UUID(int=4)),
                "http_status": 409,
                "listing_status": "generating",
                "job_id": None,
                "error": report.model_dump(mode="json"),
            },
            {
                "listing_id": str(uuid.UUID(int=5)),
                "http_status": 202,
                "listing_status": "publishing",
                "job_id": str(uuid.UUID(int=6)),
                "error": None,
            },
        ],
        "poll_url": f"/api/v1/jobs/{uuid.UUID(int=6)}",
    }
    assert svc.batch_body(batch, [refused])["poll_url"] == "/api/v1/listings"


# ---- field retry ----


def test_raise_maps_not_found_and_conflicts() -> None:
    with pytest.raises(NotFoundError) as missing:
        svc._raise(ErrorCode.NOT_FOUND)
    assert missing.value.field == "listing_id"
    with pytest.raises(ConflictError) as conflict:
        svc._raise(ErrorCode.LISTING_BUSY)
    assert (conflict.value.code, conflict.value.constraint) == (ErrorCode.LISTING_BUSY, "listings")


async def test_retry_fields_enqueues_with_the_key() -> None:
    listing = _listing(status="failed", status_reason="publish_partial")
    uow = _uow(listings__get_for_update=AsyncMock(return_value=listing))
    job_id = await svc.retry_fields(uow, _principal(), listing.id, http_key="key-2")
    assert job_id == uuid.UUID(int=99)
    uow.listings.get_for_update.assert_awaited_once_with(listing.id)
    assert _spec(uow).http_key == "key-2"
    assert _spec(uow).batch_id is None


async def test_retry_fields_refusals() -> None:
    uow = _uow()
    with pytest.raises(NotFoundError):
        await svc.retry_fields(uow, _principal(), uuid.uuid4(), http_key="k")
    uow.listings.get_for_update = AsyncMock(return_value=_listing())
    with pytest.raises(ConflictError) as conflict:
        await svc.retry_fields(uow, _principal(), uuid.uuid4(), http_key="k")
    assert conflict.value.code is ErrorCode.INVALID_STATUS_TRANSITION


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (ErrorCode.INTERNAL_ERROR, ErrorCode.INVALID_STATUS_TRANSITION),
        (ErrorCode.LISTING_BUSY, ErrorCode.LISTING_BUSY),
    ],
)
async def test_retry_fields_database_refusal(raised: ErrorCode, code: ErrorCode) -> None:
    listing = _listing(status="failed", status_reason="publish_partial")
    uow = _uow(listings__get_for_update=AsyncMock(return_value=listing))
    uow.listings.move = AsyncMock(side_effect=ConflictError(raised, "ls_guard"))
    with pytest.raises(ConflictError) as conflict:
        await svc.retry_fields(uow, _principal(), listing.id, http_key="k")
    assert conflict.value.code is code
    assert conflict.value.constraint == (
        "listings" if raised is ErrorCode.INTERNAL_ERROR else "ls_guard"
    )


# ---- refresh-status ----


@pytest.mark.parametrize(
    ("listing", "error", "code"),
    [
        (None, NotFoundError, None),
        (_listing(status="published", paused_at=T0), ConflictError, ErrorCode.LISTING_PAUSED),
        (_listing(status="published", archived_at=T0), ConflictError, ErrorCode.LISTING_ARCHIVED),
        (_listing(status="publishing"), ConflictError, ErrorCode.LISTING_BUSY),
        (_listing(status="approved"), ConflictError, ErrorCode.INVALID_STATUS_TRANSITION),
        (
            _listing(status="failed", status_reason="publish_failed"),
            ConflictError,
            ErrorCode.INVALID_STATUS_TRANSITION,
        ),
    ],
)
async def test_refresh_status_refusals(
    listing: Any, error: type[Exception], code: ErrorCode | None
) -> None:
    uow = _uow(listings__get_for_update=AsyncMock(return_value=listing))
    with pytest.raises(error) as raised:
        await svc.refresh_status(uow, _principal(), uuid.UUID(int=10), http_key="k")
    if code is None:
        assert raised.value.field == "listing_id"  # type: ignore[attr-defined]
    else:
        assert (raised.value.code, raised.value.constraint) == (code, "listings")  # type: ignore[attr-defined]
    uow.listings.get_for_update.assert_awaited_once_with(uuid.UUID(int=10))
    uow.job_runs.enqueue.assert_not_awaited()


@pytest.mark.parametrize(
    ("latest", "allowed"), [(None, False), ("error", False), ("partially_accepted", True)]
)
async def test_refresh_status_after_a_partial(latest: str | None, allowed: bool) -> None:
    listing = _listing(status="failed", status_reason="publish_partial")
    row = None if latest is None else SimpleNamespace(outcome=latest)
    uow = _uow(
        listings__get_for_update=AsyncMock(return_value=listing),
        submissions__latest=AsyncMock(return_value=row),
    )
    if allowed:
        assert await svc.refresh_status(uow, _principal(), listing.id, http_key="k") == uuid.UUID(
            int=99
        )
    else:
        with pytest.raises(ConflictError) as raised:
            await svc.refresh_status(uow, _principal(), listing.id, http_key="k")
        assert raised.value.code is ErrorCode.INVALID_STATUS_TRANSITION
    uow.submissions.latest.assert_awaited_once_with(listing.id)


async def test_refresh_status_enqueues_a_sync_and_resets_the_polls() -> None:
    principal = _principal()
    listing = _listing(status="published")
    uow = _uow(listings__get_for_update=AsyncMock(return_value=listing))
    assert await svc.refresh_status(uow, principal, listing.id, http_key="key-3") == uuid.UUID(
        int=99
    )
    uow.job_runs.live_for_listing.assert_awaited_once_with(listing.id, JobKind.LISTING_STATUS_SYNC)
    uow.job_runs.user_requested_since.assert_awaited_once_with(
        listing.id, JobKind.LISTING_STATUS_SYNC, 300
    )
    assert _spec(uow) == EnqueueSpec(
        kind=JobKind.LISTING_STATUS_SYNC,
        entity_type="listing",
        entity_id=listing.id,
        listing_id=listing.id,
        created_by=principal.user_id,
        http_key="key-3",
        payload={"v": 1, "listing_id": str(listing.id), "reason": "refresh"},
    )
    uow.listings.set_columns.assert_awaited_once_with(
        listing.id, event="refresh_requested", sync_poll_count=0, next_sync_at=None
    )


async def test_refresh_status_returns_a_live_sync_and_limits_repeats() -> None:
    listing = _listing(status="published")
    uow = _uow(
        listings__get_for_update=AsyncMock(return_value=listing),
        job_runs__live_for_listing=AsyncMock(return_value=_job(uuid.UUID(int=44))),
    )
    assert await svc.refresh_status(uow, _principal(), listing.id, http_key="k") == uuid.UUID(
        int=44
    )
    uow.job_runs.user_requested_since.assert_not_awaited()
    uow.job_runs.live_for_listing = AsyncMock(return_value=None)
    uow.job_runs.user_requested_since = AsyncMock(return_value=True)
    with pytest.raises(LimitError) as limited:
        await svc.refresh_status(uow, _principal(), listing.id, http_key="k")
    assert limited.value.code is ErrorCode.RATE_LIMITED
    assert limited.value.limit == "refresh-status 1 per 5 min per listing"
    uow.job_runs.enqueue.assert_not_awaited()


# ---- the idempotent accept wrappers ----


def svc_accepted_body(job_id: uuid.UUID, listing_id: uuid.UUID) -> dict[str, Any]:
    from app.services.generation import ItemAccepted, accepted_body

    body = accepted_body(ItemAccepted(job_id=job_id, listing_id=listing_id))
    assert body["job_id"] == str(job_id)
    assert body["listing_id"] == str(listing_id)
    return body


def _patch_idempotent(
    monkeypatch: pytest.MonkeyPatch, inner: str, job_id: uuid.UUID
) -> list[tuple[Any, ...]]:
    """`idempotent` records its arguments and runs the handler on a stub session; the inner
    service call records its own and answers `job_id`."""
    from app.services import idempotency

    calls: list[tuple[Any, ...]] = []

    async def fake_idempotent(
        principal: Any, key: str, method: str, path: str, payload: bytes, handler: Any
    ) -> tuple[int, dict[str, Any]]:
        status, body = await handler("session")
        calls.append((principal, key, method, path, payload, status))
        return status, body

    async def fake_inner(uow: Any, principal: Any, listing_id: Any, *, http_key: str) -> Any:
        calls.append(("inner", uow, listing_id, http_key))
        return job_id

    monkeypatch.setattr(idempotency, "idempotent", fake_idempotent)
    monkeypatch.setattr(svc, inner, fake_inner)
    monkeypatch.setattr(svc, "repositories_on", lambda session, tenant: ("uow", session, tenant))
    return calls


@pytest.mark.parametrize(
    ("accept", "inner", "payload"),
    [
        ("accept_retry_fields", "retry_fields", {"listing_id": "L"}),
        ("accept_refresh_status", "refresh_status", {"listing_id": "L", "op": "refresh"}),
    ],
)
async def test_accept_wrappers(
    monkeypatch: pytest.MonkeyPatch, accept: str, inner: str, payload: dict[str, str]
) -> None:
    principal = _principal()
    listing_id = uuid.UUID(int=12)
    job_id = uuid.UUID(int=13)
    calls = _patch_idempotent(monkeypatch, inner, job_id)
    body = await getattr(svc, accept)(principal, "key-9", "/api/v1/x", listing_id)
    assert body == svc_accepted_body(job_id, listing_id)
    expected = {k: (str(listing_id) if v == "L" else v) for k, v in payload.items()}
    assert calls == [
        ("inner", ("uow", "session", principal.tenant_id), listing_id, "key-9"),
        (principal, "key-9", "POST", "/api/v1/x", json.dumps(expected).encode(), 202),
    ]


async def test_accept_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import idempotency

    principal = _principal()
    items = [svc.PublishItemIn(uuid.UUID(int=4))]
    seen: list[Any] = []

    async def fake_batch(uow: Any, who: Any, got: Any, *, http_key: str) -> Any:
        seen.append((uow, who, got, http_key))
        return uuid.UUID(int=3), []

    async def fake_idempotent(
        who: Any, key: str, method: str, path: str, payload: bytes, handler: Any
    ) -> tuple[int, dict[str, Any]]:
        status, body = await handler("session")
        seen.append((who, key, method, path, payload, status))
        return status, body

    monkeypatch.setattr(idempotency, "idempotent", fake_idempotent)
    monkeypatch.setattr(svc, "publish_batch", fake_batch)
    monkeypatch.setattr(svc, "repositories_on", lambda session, tenant: ("uow", session, tenant))
    body = await svc.accept_publish(principal, "key-1", "/api/v1/listings/publish", b"{}", items)
    assert body == svc.batch_body(uuid.UUID(int=3), [])
    assert seen == [
        (("uow", "session", principal.tenant_id), principal, items, "key-1"),
        (principal, "key-1", "POST", "/api/v1/listings/publish", b"{}", 202),
    ]


async def test_plan_retry_fields_only_refuses_a_non_failed_status_with_a_retry_reason() -> None:
    uow = _uow()
    listing = _listing(status="approved", status_reason="publish_partial")
    with pytest.raises(svc.Refused) as refused:
        await svc.plan_publish(uow, listing, retry_fields_only=True)
    assert refused.value.code is ErrorCode.INVALID_STATUS_TRANSITION


async def test_plan_reads_the_pending_submission_of_this_listing() -> None:
    uow = _uow()
    listing = _listing()
    await svc.plan_publish(uow, listing)
    uow.submissions.latest.assert_awaited_once_with(listing.id)


async def test_plan_mode_follows_retry_fields_only_not_the_status() -> None:
    """retry_fields_only asks for a field retry even when the status alone would say publish;
    without it a publish_failed listing is a full publish after a partial accept."""
    for only, mode, payload_read in ((True, "retry_fields", True), (False, "publish", False)):
        uow = _uow(
            submissions__count_for_version=AsyncMock(return_value=1),
            submissions__last_for_version=AsyncMock(return_value=_prev("partially_accepted")),
        )
        listing = _listing(status="failed", status_reason="publish_failed")
        plan = await svc.plan_publish(uow, listing, retry_fields_only=only)
        assert plan.mode == mode, only
        assert uow.field_results.retry_payload.await_count == int(payload_read)
