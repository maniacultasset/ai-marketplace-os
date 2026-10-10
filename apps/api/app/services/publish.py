"""Publish, field retry and refresh-status requests (S21 T9, T11, T23; DATABASE.md :947-949, §7.1;
PLAN D2, D3, D4, D6; CP1, CP3, CP4, CP9; invariants 1, 9, 10).

`publish_batch(uow, principal, items, http_key=)` runs in the `idempotent` transaction: one
`batches` row (kind `publish`), every target listing locked in id order (`lock_many`), then each
item in its own SAVEPOINT, checked in this order, each refusal an item `ErrorReport` (the request
stays 202):

missing or another tenant's → 404; a marketplace switched off here (Flipkart with
`FLIPKART_ENABLED=false`, S22 C3, C12) → 501 NOT_IMPLEMENTED "Flipkart is not enabled", before
every other check, so the flag never answers for a foreign id and no status or account check
hides it; paused / archived → 409 LISTING_PAUSED / LISTING_ARCHIVED;
`generating|publishing` → 409 LISTING_BUSY; a `listing_version_id` that is not current → 409
STALE_VERSION (D4); the current version not
the approved one → 409 INVALID_STATUS_TRANSITION; the status has no publish event → 409
INVALID_STATUS_TRANSITION; no active account → 409 AUTH_EXPIRED (D6); then, unless the latest
submission is a `pending` row whose job ended (resolve-then-retry, CP1 (c): the new job reads the
marketplace before anything else), 3 submissions of the version → 409
SUBMISSION_ATTEMPTS_EXHAUSTED, and a retry with no justification (CP9) → 409
DUPLICATE_SUBMISSION_BLOCKED (a partial with nothing failed to resend → 409
INVALID_STATUS_TRANSITION).

An accepted item INSERTs the `listing_publish` job (`created_by` = the member, `batch_id`, payload
v1, `parent_job_id` = the listing's previous publish job for a retry, CP3), then moves the listing
to `publishing` with `publish|retry_publish|resume_publish` (the order the 0004 event rule needs),
which defers the queue entry in the same transaction, and audits `listing.publish_requested`. The
database's publish gate and status guard re-check all of it; their refusal is the item's 409. No
adapter is called in the request (invariant 1: only the job publishes).

`retry_fields` is the single-listing field retry (409s raised, not per item; a switched-off
marketplace is the whole-request 501, raised inside `idempotent`, so nothing is stored): status
`failed/publish_partial|publish_failed`, the same account, cap, resolve-then-retry and
justification checks, a non-empty retry payload, mode `retry_fields`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, NoReturn

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.core.flags import MarketplaceNotEnabled, marketplace_enabled, not_enabled_report
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.core.request_context import current_request_id
from app.repositories.jobs import EnqueueSpec
from app.repositories.listings import UserActor
from app.repositories.uow import TenantUnitOfWork, repositories_on
from app.services.publish_rules import (
    Mode,
    PreviousAttempt,
    choose_justification,
    mode_for,
    publish_event,
)
from app.workers.payloads import PublishJobPayload, dump

log = get_logger(__name__)

BUSY: Final = frozenset({"generating", "publishing"})
TERMINAL_JOB: Final = frozenset({"succeeded", "partial", "failed", "cancelled", "dead_letter"})
MAX_SUBMISSIONS: Final = 3
RETRY_FIELDS_REASONS: Final = frozenset({"publish_partial", "publish_failed"})
_STATUS: Final[Mapping[ErrorCode, int]] = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.NOT_IMPLEMENTED: 501,
    ErrorCode.INTERNAL_ERROR: 500,
}
_MESSAGES: Final[Mapping[ErrorCode, str]] = {
    ErrorCode.NOT_FOUND: "listing_id not found.",
    ErrorCode.LISTING_PAUSED: "The listing is paused.",
    ErrorCode.LISTING_ARCHIVED: "The listing is archived.",
    ErrorCode.LISTING_BUSY: "The listing is generating or publishing.",
    ErrorCode.STALE_VERSION: "The version is not the listing's current version.",
    ErrorCode.NOT_IMPLEMENTED: "Publishing to this marketplace is not available yet.",
    ErrorCode.INVALID_STATUS_TRANSITION: "The listing cannot be published in its current status.",
    ErrorCode.AUTH_EXPIRED: "Connect or reconnect the marketplace account first.",
    ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED: (
        "This version was submitted 3 times; edit or regenerate it to publish again."
    ),
    ErrorCode.DUPLICATE_SUBMISSION_BLOCKED: (
        "Nothing changed since the last attempt that would let it succeed."
    ),
    ErrorCode.SUBMISSION_IN_FLIGHT: "A submission of this listing is still open.",
    ErrorCode.INTERNAL_ERROR: "Internal error.",
}


class Refused(Exception):  # noqa: N818 - a per-item answer, not a fault
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code
        self.status = _STATUS.get(code, 409)


class Disabled(Refused):
    """The listing's marketplace is switched off here (S22 C3): item 501 with the flag's report."""

    def __init__(self, marketplace: str) -> None:
        super().__init__(ErrorCode.NOT_IMPLEMENTED)
        self.marketplace = marketplace

    def report(self, listing_id: uuid.UUID) -> ErrorReport:
        return not_enabled_report(self.marketplace).model_copy(
            update={"request_id": current_request_id(), "listing_id": listing_id}
        )


def item_report(code: ErrorCode, listing_id: uuid.UUID | None = None) -> ErrorReport:
    retryable = ERROR_CODE_META[code].retry_class is RetryClass.AUTO_RETRY
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=retryable,
        errors=[
            ErrorItem(
                field="listing",
                error_code=code,
                message=_MESSAGES.get(code, "The item was refused."),
                source=ErrorSource.SYSTEM,
                retryable=retryable,
            )
        ],
        request_id=current_request_id(),
        listing_id=listing_id,
    )


@dataclass(frozen=True, slots=True)
class PublishItemIn:
    listing_id: uuid.UUID
    listing_version_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    listing_id: uuid.UUID
    http_status: int
    listing_status: str | None = None
    job_id: uuid.UUID | None = None
    error: ErrorReport | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    """What one accepted request enqueues."""

    event: str
    mode: Mode
    parent_job_id: uuid.UUID | None


def _gate(listing: Any | None, version_id: uuid.UUID | None) -> Any:
    if listing is None:
        raise Refused(ErrorCode.NOT_FOUND)
    if not marketplace_enabled(listing.marketplace, get_settings()):
        raise Disabled(listing.marketplace)
    if listing.paused_at is not None:
        raise Refused(ErrorCode.LISTING_PAUSED)
    if listing.archived_at is not None:
        raise Refused(ErrorCode.LISTING_ARCHIVED)
    if listing.status in BUSY:
        raise Refused(ErrorCode.LISTING_BUSY)
    if version_id is not None and version_id != listing.current_version_id:
        raise Refused(ErrorCode.STALE_VERSION)
    if listing.current_version_id is None or listing.approved_version_id != (
        listing.current_version_id
    ):
        raise Refused(ErrorCode.INVALID_STATUS_TRANSITION)
    return listing


async def _account(uow: TenantUnitOfWork, listing: Any) -> uuid.UUID:
    account = await uow.marketplace_accounts.active_for_publish(
        listing.marketplace, listing.marketplace_account_id
    )
    if account is None:
        raise Refused(ErrorCode.AUTH_EXPIRED)
    return account


async def unresolved_pending(uow: TenantUnitOfWork, listing_id: uuid.UUID) -> Any | None:
    """The latest submission when it is `pending` and its job has ended (CP1 (c))."""
    latest = await uow.submissions.latest(listing_id)
    if latest is None or latest.outcome != "pending":
        return None
    job = await uow.job_runs.get_by_id(latest.job_id)
    if job is not None and job.status not in TERMINAL_JOB:
        raise Refused(ErrorCode.LISTING_BUSY)
    return latest


async def justification_for(uow: TenantUnitOfWork, listing: Any, prev: Any) -> str | None:
    """CP9 over the version's last attempt."""
    applied = False
    if prev.resolved_at is not None:
        tasks = await uow.listing_issues.task_ids_for_job(prev.job_id)
        applied = await uow.human_tasks.any_applied_after(tasks, prev.resolved_at)
    moved = (
        listing.marketplace_state_changed_at is not None
        and prev.resolved_at is not None
        and listing.marketplace_state_changed_at > prev.resolved_at
    )
    return choose_justification(
        PreviousAttempt(prev.outcome, prev.error_report_json, prev.resolved_at),
        applied_task_after=applied,
        marketplace_changed_after=moved,
    )


async def plan_publish(
    uow: TenantUnitOfWork, listing: Any, *, retry_fields_only: bool = False
) -> Plan:
    """The publish checks after the gate (module docstring order)."""
    if listing.status == "blocked" and listing.status_reason == "publish_attempts_exhausted":
        raise Refused(ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED)
    event = publish_event(listing.status, listing.status_reason)
    if event is None or (
        retry_fields_only
        and not (listing.status == "failed" and listing.status_reason in RETRY_FIELDS_REASONS)
    ):
        raise Refused(ErrorCode.INVALID_STATUS_TRANSITION)
    await _account(uow, listing)
    parent = (
        await uow.listings.latest_job_id(listing.id, JobKind.LISTING_PUBLISH)
        if event != "publish"
        else None
    )
    if await unresolved_pending(uow, listing.id) is not None:
        return Plan(event=event, mode="publish", parent_job_id=parent)
    version = listing.approved_version_id
    sent = await uow.submissions.count_for_version(listing.id, version)
    if sent >= MAX_SUBMISSIONS:
        raise Refused(ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED)
    mode: Mode = (
        "retry_fields" if retry_fields_only else mode_for(listing.status, listing.status_reason)
    )
    if sent == 0:
        return Plan(
            event=event, mode="publish" if mode == "retry_fields" else mode, parent_job_id=parent
        )
    prev = await uow.submissions.last_for_version(listing.id, version)
    if prev is None:  # unreachable: `sent` counted it
        raise Refused(ErrorCode.INVALID_STATUS_TRANSITION)
    if mode == "retry_fields":
        if prev.outcome != "partially_accepted":
            mode = "publish"
        elif not await uow.field_results.retry_payload(listing.id, version):
            raise Refused(ErrorCode.INVALID_STATUS_TRANSITION)
    if prev.outcome in ("accepted", "live", "superseded", "pending"):
        raise Refused(ErrorCode.INVALID_STATUS_TRANSITION)
    if await justification_for(uow, listing, prev) is None:
        raise Refused(ErrorCode.DUPLICATE_SUBMISSION_BLOCKED)
    return Plan(event=event, mode=mode, parent_job_id=parent)


def _gate_refusal(exc: DomainError) -> ErrorCode:
    if isinstance(exc, NotFoundError):
        return ErrorCode.NOT_FOUND
    if isinstance(exc, ConflictError):
        # the 0004 publish gate raises GRD01 (INTERNAL_ERROR): its refusal is a status 409
        if exc.code is ErrorCode.INTERNAL_ERROR:
            return ErrorCode.INVALID_STATUS_TRANSITION
        return exc.code
    return ErrorCode.INTERNAL_ERROR


async def enqueue_publish(
    uow: TenantUnitOfWork,
    principal: Principal,
    listing: Any,
    plan: Plan,
    *,
    http_key: str | None,
    batch_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """INSERT the job, then the status move (the 0004 event rule needs the queued job first)."""
    # the gate checked approved = current; the database's publish gate checks it again
    version = listing.approved_version_id or listing.current_version_id
    payload = PublishJobPayload(listing_id=listing.id, listing_version_id=version, mode=plan.mode)
    enqueued = await uow.job_runs.enqueue(
        EnqueueSpec(
            kind=JobKind.LISTING_PUBLISH,
            entity_type="listing",
            entity_id=listing.id,
            listing_id=listing.id,
            created_by=principal.user_id,
            http_key=http_key,
            payload=dump(payload),
            batch_id=batch_id,
            parent_job_id=plan.parent_job_id,
        )
    )
    if not enqueued.created:
        raise Refused(ErrorCode.LISTING_BUSY)
    from_status = listing.status
    await uow.listings.move(
        listing, to="publishing", event=plan.event, actor=UserActor(principal.user_id)
    )
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action="listing.publish_requested",
        entity_type="listing",
        entity_id=listing.id,
        before=json.dumps({"status": from_status}),
        after=json.dumps({"status": "publishing"}),
        reason=plan.event,
    )
    log.info(
        "publish.requested",
        listing_id=str(listing.id),
        job_id=str(enqueued.job.id),
        listing_event=plan.event,
        mode=plan.mode,
    )
    return enqueued.job.id


async def _one(
    uow: TenantUnitOfWork,
    principal: Principal,
    listings: Mapping[uuid.UUID, Any],
    item: PublishItemIn,
    *,
    http_key: str,
    batch_id: uuid.UUID,
) -> ItemOutcome:
    listing = listings.get(item.listing_id)
    status: str | None = None if listing is None else listing.status
    try:
        async with uow.session.begin_nested():
            _gate(listing, item.listing_version_id)
            plan = await plan_publish(uow, listing)
            job_id = await enqueue_publish(
                uow, principal, listing, plan, http_key=http_key, batch_id=batch_id
            )
            return ItemOutcome(item.listing_id, 202, "publishing", job_id)
    except Disabled as disabled:
        return ItemOutcome(
            item.listing_id, disabled.status, status, error=disabled.report(item.listing_id)
        )
    except Refused as refused:
        code = refused.code
    except DomainError as exc:
        code = _gate_refusal(exc)
        if code is ErrorCode.INTERNAL_ERROR:
            raise
    refusal = Refused(code)
    return ItemOutcome(
        item.listing_id, refusal.status, status, error=item_report(code, item.listing_id)
    )


async def publish_batch(
    uow: TenantUnitOfWork,
    principal: Principal,
    items: Sequence[PublishItemIn],
    *,
    http_key: str,
) -> tuple[uuid.UUID, list[ItemOutcome]]:
    if not items:
        raise ValueError("a publish batch has at least one item")
    batch = await uow.batches.insert(
        "publish", len(items), principal.user_id, request_id=current_request_id()
    )
    listings = await uow.listings.lock_many([i.listing_id for i in items])
    outcomes = [
        await _one(uow, principal, listings, item, http_key=http_key, batch_id=batch.id)
        for item in items
    ]
    return batch.id, outcomes


def batch_body(batch_id: uuid.UUID, outcomes: Sequence[ItemOutcome]) -> dict[str, Any]:
    """The 202 `BatchAccepted` body: per-item results; `poll_url` names the first job."""
    first = next((o.job_id for o in outcomes if o.job_id is not None), None)
    return {
        "batch_id": str(batch_id),
        "items": [
            {
                "listing_id": str(o.listing_id),
                "http_status": o.http_status,
                "listing_status": o.listing_status,
                "job_id": None if o.job_id is None else str(o.job_id),
                "error": None if o.error is None else o.error.model_dump(mode="json"),
            }
            for o in outcomes
        ],
        "poll_url": f"/api/v1/jobs/{first}" if first is not None else "/api/v1/listings",
    }


async def accept_publish(
    principal: Principal, key: str, path: str, payload: bytes, items: Sequence[PublishItemIn]
) -> dict[str, Any]:
    """The route's idempotent transaction: 202 with per-item results, stored under the key."""
    from app.services.idempotency import idempotent

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        batch_id, outcomes = await publish_batch(uow, principal, items, http_key=key)
        return 202, batch_body(batch_id, outcomes)

    _status, stored = await idempotent(principal, key, "POST", path, payload, handler)
    return stored


# ---- field retry (T11) ----


def _raise(code: ErrorCode) -> NoReturn:
    if code is ErrorCode.NOT_FOUND:
        raise NotFoundError("listing_id")
    raise ConflictError(code, "listings")


async def retry_fields(
    uow: TenantUnitOfWork, principal: Principal, listing_id: uuid.UUID, *, http_key: str
) -> uuid.UUID:
    listing = await uow.listings.get_for_update(listing_id)
    try:
        _gate(listing, None)
        plan = await plan_publish(uow, listing, retry_fields_only=True)
        return await enqueue_publish(uow, principal, listing, plan, http_key=http_key)
    except Disabled as disabled:
        raise MarketplaceNotEnabled(disabled.marketplace) from None
    except Refused as refused:
        _raise(refused.code)
    except ConflictError as exc:
        if exc.code is ErrorCode.INTERNAL_ERROR:
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listings") from exc
        raise


async def accept_retry_fields(
    principal: Principal, key: str, path: str, listing_id: uuid.UUID
) -> dict[str, Any]:
    from app.services.generation import ItemAccepted, accepted_body
    from app.services.idempotency import idempotent

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        job_id = await retry_fields(uow, principal, listing_id, http_key=key)
        return 202, accepted_body(ItemAccepted(job_id=job_id, listing_id=listing_id))

    payload = json.dumps({"listing_id": str(listing_id)}).encode()
    _status, stored = await idempotent(principal, key, "POST", path, payload, handler)
    return stored


# ---- refresh-status (T23; D3, CP4, CP7) ----

REFRESH_WINDOW_S: Final = 300


async def refresh_status(
    uow: TenantUnitOfWork, principal: Principal, listing_id: uuid.UUID, *, http_key: str
) -> uuid.UUID:
    """Check the marketplace now: a `listing_status_sync` job with the poll count reset.
    Allowed on `published` (any marketplace state) and on `failed/publish_partial` after a
    partial accept (D7); `publishing` → 409 LISTING_BUSY; anything else → 409
    INVALID_STATUS_TRANSITION (the sync has no edge there; recovery is a publish). One request
    per listing per 5 minutes (429 RATE_LIMITED); a live sync job is returned as is."""
    from app.core.errors import LimitError
    from app.workers.payloads import StatusSyncPayload

    listing = await uow.listings.get_for_update(listing_id)
    if listing is None:
        raise NotFoundError("listing_id")
    if listing.paused_at is not None:
        raise ConflictError(ErrorCode.LISTING_PAUSED, "listings")
    if listing.archived_at is not None:
        raise ConflictError(ErrorCode.LISTING_ARCHIVED, "listings")
    if listing.status == "publishing":
        raise ConflictError(ErrorCode.LISTING_BUSY, "listings")
    allowed = listing.status == "published"
    if listing.status == "failed" and listing.status_reason == "publish_partial":
        latest = await uow.submissions.latest(listing.id)
        allowed = latest is not None and latest.outcome == "partially_accepted"
    if not allowed:
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listings")
    live = await uow.job_runs.live_for_listing(listing.id, JobKind.LISTING_STATUS_SYNC)
    if live is not None:
        return live.id
    if await uow.job_runs.user_requested_since(
        listing.id, JobKind.LISTING_STATUS_SYNC, REFRESH_WINDOW_S
    ):
        raise LimitError("refresh-status 1 per 5 min per listing")
    enqueued = await uow.job_runs.enqueue(
        EnqueueSpec(
            kind=JobKind.LISTING_STATUS_SYNC,
            entity_type="listing",
            entity_id=listing.id,
            listing_id=listing.id,
            created_by=principal.user_id,
            http_key=http_key,
            payload=dump(StatusSyncPayload(listing_id=listing.id, reason="refresh")),
        )
    )
    await uow.listings.set_columns(
        listing.id, event="refresh_requested", sync_poll_count=0, next_sync_at=None
    )
    log.info("sync.refresh_requested", listing_id=str(listing.id), job_id=str(enqueued.job.id))
    return enqueued.job.id


async def accept_refresh_status(
    principal: Principal, key: str, path: str, listing_id: uuid.UUID
) -> dict[str, Any]:
    from app.services.generation import ItemAccepted, accepted_body
    from app.services.idempotency import idempotent

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        job_id = await refresh_status(uow, principal, listing_id, http_key=key)
        return 202, accepted_body(ItemAccepted(job_id=job_id, listing_id=listing_id))

    payload = json.dumps({"listing_id": str(listing_id), "op": "refresh"}).encode()
    _status, stored = await idempotent(principal, key, "POST", path, payload, handler)
    return stored
