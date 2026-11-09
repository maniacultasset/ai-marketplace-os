"""The writes of one status sync (S21 T21; DATABASE.md :756, :764, :770; PLAN D7; CP5).

`apply_sync(uow, job_id, listing_id, reads)` runs in the wrapper's fenced terminal transaction
(the job's `persist`), under the listing lock. It writes nothing while the listing is publishing or
no longer submitted (a later publish owns it), else applies `sync_decision`: the items' states
(`live` with the job GUC, `li_guard`), the listing's `marketplace_state`, `live_*` and schedule
columns, and either an event (`sync` only when a state changed, so the daily reconciliation stays
off the last-activity index) or a status move, and the open submission's settlement, all in one
transaction. `count_failed_poll` is the terminal hook's part: a dead-lettered sync job still
counts its poll and reschedules (or exhausts; SI9: a not-buyable listing never exhausts).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind, Severity
from mkos_schemas.errors import MAX_ERROR_ITEMS, ErrorItem, ErrorReport

from app.adapters.registry import live_url
from app.core.logging import get_logger
from app.db.models.ops import JobRun
from app.repositories.listings import DbNow, JobActor, ListingRepository
from app.repositories.uow import TenantUnitOfWork, repositories_on
from app.services.issues import adapter_source, dedupe_key, sync_issues
from app.services.resolution import reassign
from app.services.resolution.reassign import CATEGORY_PATH
from app.services.status_sync import (
    DAILY,
    MAX_POLLS,
    ItemRead,
    SyncDecision,
    next_slot,
    spread,
    sync_decision,
)
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


async def _settle(
    uow: TenantUnitOfWork,
    listing: Any,
    decision: SyncDecision,
    errors: Sequence[ErrorItem] = (),
) -> None:
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
        # SI4 A17: the read's items ride on the rejection's report (one ErrorReport shape)
        blocking = [e for e in errors if e.severity is Severity.ERROR]
        code = blocking[0].error_code if blocking else ErrorCode.PUBLISH_FAILED
        report = ErrorReport(status="failed", error_code=code, errors=list(errors))
        await uow.submissions.settle_accepted(
            open_row.id,
            outcome="rejected",
            error_code=code.value,
            report=report.model_dump(mode="json"),
        )
    else:
        await uow.submissions.settle_accepted(open_row.id, outcome="live")


def _merged_reads(
    listing: Any, reads: Sequence[ItemRead], rows: Mapping[str, Any]
) -> tuple[list[ErrorItem], dict[str, uuid.UUID], list[str]]:
    """SI4 A17 (CP-D16): every read of the family as one report. Items dedupe by key (the first
    read naming one wins, and its SKU's item id rides on a new row); the scope is every path any
    read read or named, plus `listing`."""
    source = adapter_source(listing.marketplace)
    merged: dict[str, ErrorItem] = {}
    item_ids: dict[str, uuid.UUID] = {}
    for read in reads:
        row = rows.get(read.seller_sku)
        for error in read.errors:
            key = dedupe_key(source, error)
            if key in merged:
                continue
            merged[key] = error
            if row is not None:
                item_ids[key] = row.id
    scope = {"listing"}
    for read in reads:
        scope |= set(read.fields) | {e.field for e in read.errors}
    # a read now carries every issue (round 3 F2): the one report holds at most MAX_ERROR_ITEMS
    return list(merged.values())[:MAX_ERROR_ITEMS], item_ids, sorted(scope)


async def _sync_read_issues(
    uow: TenantUnitOfWork,
    job_id: uuid.UUID,
    listing: Any,
    errors: Sequence[ErrorItem],
    item_ids: Mapping[str, uuid.UUID],
    scope: Sequence[str],
    *,
    close: bool,
) -> None:
    """One `sync_issues` per sync (CP-D16): a clean read clears a published listing's open
    adapter rows on the scoped fields (`marketplace_cleared`, CP-D13); a `failed` listing's sync
    opens and repeats but never closes (its read may show an older live version or nothing)."""
    failed = any(e.severity is Severity.ERROR for e in errors)
    await sync_issues(
        uow,
        listing,
        adapter_source(listing.marketplace),
        ErrorReport(status="failed" if failed else "passed", errors=list(errors)),
        run_id=None,
        cause="status_sync",
        scope_fields=scope,
        job_id=job_id,
        critical_keys=await uow.product_facts.critical_keys(),
        listing_item_ids=item_ids,
        close=close,
    )


async def _category_signal(
    uow: TenantUnitOfWork, listing: Any, reads: Sequence[ItemRead], merged: Sequence[ErrorItem]
) -> ErrorItem | None:
    """SI13 T6 (`resolution.reassign`): Amazon filing the item under another product type, or a
    burst of missing attributes that belong to another type, is one `CATEGORY_MISMATCH` item at
    `facts.category` on the read's own items (CP-D10)."""
    if listing.marketplace != "amazon":
        return None
    read_type = next((r.product_type for r in reads if not r.parent and r.product_type), None)
    read_type = read_type or next((r.product_type for r in reads if r.product_type), None)
    version_id = listing.published_version_id or listing.current_version_id
    version = None if version_id is None else await uow.listing_versions.get(listing.id, version_id)
    sent_type = None
    if version is not None and version.category_template_id is not None:
        pinned = await uow.templates.pinned(version.category_template_id)
        sent_type = None if pinned is None else pinned.product_type
    missing_now = sum(
        1
        for e in merged
        if e.error_code is ErrorCode.MISSING_ATTRIBUTE and e.severity is Severity.ERROR
    )
    held = await uow.listing_issues.open_errors(
        listing.id, [adapter_source(listing.marketplace)], channel="live"
    )
    missing_before = sum(1 for r in held if r.error_code == ErrorCode.MISSING_ATTRIBUTE.value)
    item_open = any(
        r.error_code == ErrorCode.CATEGORY_MISMATCH.value and r.field_path == CATEGORY_PATH
        for r in held
    )
    found = reassign.signal(
        read_type,
        sent_type,
        missing_now,
        accepted_before=any(r.state is not None for r in reads),
        item_open=item_open,
        missing_before=missing_before,
    )
    if found is None:
        return None
    suggested = None
    if found == "type_change" and read_type is not None:
        options = await uow.templates.categories_for_product_type(listing.marketplace, read_type)
        if len(options) == 1 and options[0] != listing.category:
            suggested = options[0]
    log.info("sync.category_signal", listing_id=str(listing.id), signal=found)
    return reassign.category_item(found, read_type, suggested)


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
    published = listing.status == "published"  # read before any move (CP-D16)
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
    root_url: str | None = None
    root_external: str | None = None
    for read in reads:
        row = rows.get(read.seller_sku)
        if row is None:
            continue
        state = decision.items.get(read.seller_sku, row.marketplace_state)
        values: dict[str, Any] = {"last_synced_at": DbNow()}
        if state != row.marketplace_state:
            values["marketplace_state"] = state
        external = row.external_id or (read.external_id[:128] if read.external_id else None)
        if read.external_id and row.external_id is None:
            values["external_id"] = external
        if state == "live" and row.marketplace_state != "live":
            values["live_version_id"] = version
            values["live_confirmed_at"] = DbNow()
        url = row.live_url
        # SI9 B3: a not-buyable read has a product page too, so it gets its url and id
        on_marketplace = state == "live" or (read.not_buyable and state == "inactive")
        if on_marketplace and url is None:  # the adapter builds it (DATABASE.md:757)
            url = live_url(listing.marketplace, external)
            if url is not None:
                values["live_url"] = url
        if row.item_role in ("standalone", "parent") and on_marketplace:
            root_url, root_external = url, external
        await uow.listing_items.write_state(row.id, job_id=job_id, **values)
    merged, item_ids, scope = _merged_reads(listing, reads, rows)
    category = await _category_signal(uow, listing, reads, merged)
    if category is not None:
        merged = [*merged, category][:MAX_ERROR_ITEMS]
    if listing.marketplace == "amazon":
        scope = sorted({*scope, CATEGORY_PATH})  # so a read without the signal closes the item
    await _settle(uow, listing, decision, merged)
    await _sync_read_issues(uow, job_id, listing, merged, item_ids, scope, close=published)
    columns: dict[str, Any] = {
        "last_synced_at": DbNow(),
        "sync_poll_count": decision.poll_count,
        "next_sync_at": None if decision.next_in is None else DbNow(spread(decision.next_in)),
    }
    changed = decision.marketplace_state != listing.marketplace_state
    if changed:
        columns["marketplace_state"] = decision.marketplace_state
        columns["marketplace_state_changed_at"] = DbNow()
    if decision.stamp_live:
        columns["live_version_id"] = version
        if root_url is not None and listing.live_url is None:
            columns["live_url"] = root_url
        if root_external is not None and listing.external_id is None:
            columns["external_id"] = root_external
        if listing.live_confirmed_at is None or listing.live_version_id != version:
            columns["live_confirmed_at"] = DbNow()
    elif decision.not_buyable:
        # CP-D7: the url and the ASIN only; live_version_id / live_confirmed_at wait for live
        if root_url is not None and listing.live_url is None:
            columns["live_url"] = root_url
        if root_external is not None and listing.external_id is None:
            columns["external_id"] = root_external
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
        await uow.listings.set_columns(listing.id, next_sync_at=DbNow(spread(DAILY)))
        return
    if listing.marketplace_state == "inactive":
        # SI9 B3 (CP-D9): published + inactive is the not-buyable state: count up to the 9th
        # poll, then daily; never exhausted
        if listing.sync_poll_count < MAX_POLLS:
            count = listing.sync_poll_count + 1
            await uow.listings.set_columns(
                listing.id,
                sync_poll_count=count,
                next_sync_at=DbNow(spread(next_slot(count) or DAILY)),
            )
        else:
            await uow.listings.set_columns(listing.id, next_sync_at=DbNow(spread(DAILY)))
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
