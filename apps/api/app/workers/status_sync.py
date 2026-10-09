"""The `listing_status_sync` job body (S21 T21; DATABASE.md :764, :770, :890; PLAN D7; CP14;
DW7; invariant 1).

1. One short read transaction (`created_by` NULL for a scheduled sync, so no user GUC): the
   listing, its account and its items. A listing that is publishing, paused, archived or not
   submitted is skipped (the outcome says so; nothing is written).
2. Outside any transaction: one `ReaderView.get_listing` per SKU. The sync only ever holds a
   reader (`open_reader`), never a mutating adapter; a failed read raises `RetryableJobError`.
3. The writes happen in the wrapper's fenced terminal transaction (`persist`,
   `status_sync_outcome.apply_sync`), which re-reads the listing under its lock.
"""

from __future__ import annotations

from typing import Final

from mkos_schemas.codes import ErrorCode

from app.core.logging import get_logger
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.status_sync import ItemRead
from app.services.status_sync_outcome import apply_sync
from app.workers.errors import RetryableJobError
from app.workers.payloads import StatusSyncPayload
from app.workers.readers import ReadFailed, open_reader
from app.workers.registry import JobContext, Outcome

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
        account = listing.marketplace_account_id
        items = [
            (row.seller_sku, row.item_role == "parent")
            for row in await uow.listing_items.for_listing(listing.id)
        ]
    if not items:
        return Outcome("succeeded", result={"skipped": True})
    reader = await open_reader(ctx.tenant_id, account)
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


def _state(value: object) -> str:
    """A listing-level read state as an item state (`partially_live` never reads for one SKU)."""
    text = str(getattr(value, "value", value) or "processing")
    return "live" if text == "partially_live" else text
