"""The writes of one status sync (S21 T21; DATABASE.md :756, :764, :770; PLAN D7; CP5).

`apply_sync(uow, job_id, listing_id, reads)` runs in the wrapper's fenced terminal transaction
(the job's `persist`), under the listing lock. It writes nothing while the listing is publishing or
no longer submitted (a later publish owns it), else applies `sync_decision`: the items' states
(`live` with the job GUC, `li_guard`), the listing's `marketplace_state`, `live_*` and schedule
columns, and either an event (`sync` only when a state changed, so the daily reconciliation stays
off the last-activity index) or a status move, and the open submission's settlement, all in one
transaction. `count_failed_poll` is the terminal hook's part: a dead-lettered sync job still
counts its poll and reschedules (or exhausts).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.logging import get_logger
from app.db.models.ops import JobRun
from app.repositories.listings import DbNow, JobActor, ListingRepository
from app.repositories.uow import TenantUnitOfWork, repositories_on
from app.services.status_sync import DAILY, ItemRead, SyncDecision, next_slot, sync_decision
from app.workers.registry import Outcome

log = get_logger(__name__)

SYNCABLE: Final = frozenset({"published", "failed"})
ENDED: Final = frozenset({"failed", "dead_letter"})


def _unconfirmed() -> ErrorReport:
    item = ErrorItem(
        field="listing",
        error_code=ErrorCode.MARKETPLACE_UNCONFIRMED,
        message="The marketplace did not confirm the listing in time; publish again to retry.",
        source=ErrorSource.SYSTEM,
        retryable=True,
    )
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.MARKETPLACE_UNCONFIRMED,
        retryable=True,
        errors=[item],
    )


async def _settle(uow: TenantUnitOfWork, listing: Any, decision: SyncDecision) -> None:
    if decision.submission_to is None:
        return
    open_row = await uow.submissions.open_for_listing(listing.id)
    if open_row is None or open_row.outcome != "accepted":
        return
    if decision.submission_to == "error":
        report = _unconfirmed()
        await uow.submissions.settle_accepted(
            open_row.id,
            outcome="error",
            error_code=ErrorCode.MARKETPLACE_UNCONFIRMED.value,
            report=report.model_dump(mode="json"),
        )
    elif decision.submission_to == "rejected":
        report = ErrorReport(status="failed", error_code=ErrorCode.PUBLISH_FAILED)
        await uow.submissions.settle_accepted(
            open_row.id,
            outcome="rejected",
            error_code=ErrorCode.PUBLISH_FAILED.value,
            report=report.model_dump(mode="json"),
        )
    else:
        await uow.submissions.settle_accepted(open_row.id, outcome="live")


async def apply_sync(
    uow: TenantUnitOfWork, job_id: uuid.UUID, listing_id: uuid.UUID, reads: Sequence[ItemRead]
) -> SyncDecision | None:
    listing = await uow.listings.get_for_update(listing_id)
    if (
        listing is None
        or listing.status not in SYNCABLE
        or listing.paused_at
        or listing.archived_at
    ):
        return None
    rows = {r.seller_sku: r for r in await uow.listing_items.for_listing(listing.id)}
    open_row = await uow.submissions.open_for_listing(listing.id)
    decision = sync_decision(
        status=listing.status,
        reason=listing.status_reason,
        current_state=listing.marketplace_state,
        poll_count=listing.sync_poll_count,
        reads=reads,
        previous={sku: r.marketplace_state for sku, r in rows.items()},
        submission_open=open_row is not None and open_row.outcome == "accepted",
    )
    if decision.marketplace_state is None:
        return decision
    version = listing.published_version_id or listing.approved_version_id
    for read in reads:
        row = rows.get(read.seller_sku)
        if row is None:
            continue
        state = decision.items.get(read.seller_sku, row.marketplace_state)
        values: dict[str, Any] = {"last_synced_at": DbNow()}
        if state != row.marketplace_state:
            values["marketplace_state"] = state
        if read.external_id and row.external_id is None:
            values["external_id"] = read.external_id[:128]
        if state == "live" and row.marketplace_state != "live":
            values["live_version_id"] = version
            values["live_confirmed_at"] = DbNow()
        await uow.listing_items.write_state(row.id, job_id=job_id, **values)
    await _settle(uow, listing, decision)
    columns: dict[str, Any] = {
        "last_synced_at": DbNow(),
        "sync_poll_count": decision.poll_count,
        "next_sync_at": None if decision.next_in is None else DbNow(decision.next_in),
    }
    changed = decision.marketplace_state != listing.marketplace_state
    if changed:
        columns["marketplace_state"] = decision.marketplace_state
        columns["marketplace_state_changed_at"] = DbNow()
    if decision.stamp_live:
        columns["live_version_id"] = version
        if listing.live_confirmed_at is None or listing.live_version_id != version:
            columns["live_confirmed_at"] = DbNow()
    if decision.listing_to is not None and decision.event is not None:
        columns["next_sync_at"] = None
        await uow.listings.move(
            listing,
            to=decision.listing_to,
            event=decision.event,
            reason=decision.reason,
            actor=JobActor(job_id),
            columns=columns,
        )
    else:
        event = "sync" if (changed and listing.status == "published") else None
        await uow.listings.set_columns(listing.id, event=event, job_id=job_id, **columns)
    log.info(
        "sync.applied",
        job_run_id=str(job_id),
        listing_id=str(listing.id),
        marketplace_state=decision.marketplace_state,
        listing_event=decision.event,
        polls=decision.poll_count,
    )
    return decision


async def count_failed_poll(session: Any, job: JobRun, outcome: Outcome | None) -> None:
    """The `listing_status_sync` terminal hook: a failed or dead-lettered sync counts its poll
    (DATABASE.md:770) and reschedules, or exhausts the schedule; `outcome` is never read."""
    del outcome
    if job.status not in ENDED or job.listing_id is None:
        return
    uow = repositories_on(session, job.tenant_id)
    listing = await uow.listings.get_for_update(job.listing_id)
    if listing is None or listing.status != "published" or listing.next_sync_at is not None:
        return
    if listing.marketplace_state in ("live", "partially_live"):
        await uow.listings.set_columns(listing.id, next_sync_at=DbNow(DAILY))
        return
    count = listing.sync_poll_count + 1
    gap = next_slot(count)
    if gap is not None:
        await uow.listings.set_columns(listing.id, sync_poll_count=count, next_sync_at=DbNow(gap))
        return
    decision = SyncDecision(submission_to="error")
    await _settle(uow, listing, decision)
    await ListingRepository(session, job.tenant_id).move(
        listing,
        to="blocked",
        event="sync_exhausted",
        reason="marketplace_action_required",
        actor=JobActor(job.id),
        columns={"sync_poll_count": count, "next_sync_at": None},
    )


async def refuse_requeue(session: Any, old: JobRun, new: JobRun) -> None:
    from app.repositories.jobs import NotEligibleError

    del session, new
    log.info("sync.requeue_refused", job_run_id=str(old.id), kind=JobKind.LISTING_STATUS_SYNC.value)
    raise NotEligibleError("the scheduler enqueues the next sync")
