"""The `listing_status_sync` job body (S21 T21; DATABASE.md :764, :770, :890; PLAN D7; CP14;
DW7; invariant 1).

1. One short read transaction (`created_by` NULL for a scheduled sync, so no user GUC): the
   listing, its account and its items. A listing that is publishing, paused, archived or not
   submitted is skipped (the outcome says so; nothing is written), and so is a listing whose
   marketplace is switched off here (Flipkart with `FLIPKART_ENABLED=false`, S22 T21), or whose
   account is not `active` (revoked or `needs_reconnect`): no read, and the only write puts back
   the sync slot the enqueue cleared (`next_sync_at` NULL -> now), so the scheduler, which skips
   both, picks the listing up again once the flag is on (S22b F5) or the listing is on an active
   account (a reconnect, or a new account adopting it after a revoke).
2. Outside any transaction: one `ReaderView.get_listing` per SKU. The sync only ever holds a
   reader (`open_reader`), never a mutating adapter; a failed read raises `RetryableJobError`,
   and an account that refuses to open (it turned non-active after step 1) fails the job with
   AUTH_EXPIRED.
3. The writes happen in the wrapper's fenced terminal transaction (`persist`,
   `status_sync_outcome.apply_sync`), which re-reads the listing under its lock.
"""

from __future__ import annotations

import uuid
from typing import Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.config import get_settings
from app.core.flags import marketplace_enabled
from app.core.logging import get_logger
from app.repositories.listings import DbNow
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.status_sync import ItemRead
from app.services.status_sync_outcome import apply_sync
from app.workers.errors import JobFailed, RetryableJobError
from app.workers.payloads import StatusSyncPayload
from app.workers.readers import AccountInactive, ReadFailed, open_reader
from app.workers.registry import JobContext, Outcome, Persist

log = get_logger(__name__)
SYNCABLE: Final = frozenset({"published", "failed"})


async def run_listing_status_sync(ctx: JobContext) -> Outcome:
    payload = StatusSyncPayload.model_validate(ctx.payload)
    async with tenant_uow(ctx.tenant_id) as uow:
        listing = await uow.listings.get(payload.listing_id)
        if (
            listing is None
            or listing.status not in SYNCABLE
            or listing.paused_at is not None
            or listing.archived_at is not None
            or listing.marketplace_account_id is None
        ):
            return Outcome("succeeded", result={"skipped": True})
        if not marketplace_enabled(listing.marketplace, get_settings()):
            log.info("sync.marketplace_disabled", listing_id=str(listing.id))
            return Outcome("succeeded", result={"skipped": True}, persist=_keep_due(listing.id))
        account = listing.marketplace_account_id
        state = await uow.marketplace_accounts.active_by_id(account)
        if state is None or state.status != "active":
            # revoked or needs_reconnect: no read; the slot stays due for when the listing is
            # on an active account again (a reconnect, or a new account adopting it)
            log.info("sync.account_not_active", listing_id=str(listing.id))
            return Outcome("succeeded", result={"skipped": True}, persist=_keep_due(listing.id))
        items = [
            (row.seller_sku, row.item_role == "parent")
            for row in await uow.listing_items.for_listing(listing.id)
        ]
    if not items:
        return Outcome("succeeded", result={"skipped": True})
    try:
        reader = await open_reader(ctx.tenant_id, account)
    except AccountInactive:
        raise JobFailed(_auth_expired()) from None
    except ReadFailed:
        raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE) from None
    reads: list[ItemRead] = []
    try:
        for sku, parent in items:
            try:
                got = await reader.get_listing(sku, listing_id=payload.listing_id)
            except ReadFailed:
                raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE) from None
            reads.append(
                ItemRead(
                    seller_sku=sku,
                    state=None if got is None else _state(got.marketplace_state),
                    external_id=None if got is None else got.external_id,
                    parent=parent,
                )
            )
    finally:
        await reader.aclose()

    async def persist(uow: TenantUnitOfWork) -> None:
        await apply_sync(uow, ctx.job_run_id, payload.listing_id, reads)

    log.info("sync.read", job_run_id=str(ctx.job_run_id), skus=len(reads), reason=payload.reason)
    return Outcome("succeeded", result={"skus": len(reads)}, persist=persist)


def _auth_expired() -> ErrorReport:
    message = "The marketplace account must reconnect."
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.AUTH_EXPIRED,
        retryable=False,
        errors=[
            ErrorItem(
                field="listing",
                error_code=ErrorCode.AUTH_EXPIRED,
                message=message,
                source=ErrorSource.SYSTEM,
                retryable=False,
            )
        ],
    )


def _keep_due(listing_id: uuid.UUID) -> Persist:
    """The skip's write: a refresh-status or a scheduler enqueue cleared `next_sync_at`; put the
    slot back (due now) under the listing lock, unless something rescheduled it meanwhile."""

    async def persist(uow: TenantUnitOfWork) -> None:
        listing = await uow.listings.get_for_update(listing_id)
        if (
            listing is None
            or listing.next_sync_at is not None
            or listing.status not in SYNCABLE
            or listing.paused_at is not None
            or listing.archived_at is not None
        ):
            return
        await uow.listings.set_columns(listing_id, next_sync_at=DbNow())

    return persist


def _state(value: object) -> str:
    """A listing-level read state as an item state: an item with failing attributes
    (`partially_live`) is not confirmed live and stays `processing`."""
    text = str(getattr(value, "value", value) or "processing")
    return "processing" if text == "partially_live" else text
