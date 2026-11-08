"""The dry-run body of `listing_status_sync` (`op = "preview"`; SI11 T7; ADR-0028 proposed).

1. One short read transaction: the listing, its current version's adapter input (no images,
   CP-D23: a preview never makes or sends a public image copy) and the live repairs. A version
   that is no longer current, a listing no longer pre-approval, or an account that is not
   `active` ends the job `{"skipped": ...}` with no marketplace call.
2. Outside any transaction: `PreviewView.preview_remote` (the only marketplace call this body can
   make; `.importlinter` `remote-preview-preview-only`). A failed call raises `RetryableJobError`
   (the job's retry; a dead letter marks the check `failed`), an account that refuses to open
   fails the job with AUTH_EXPIRED.
3. The writes run in the wrapper's fenced terminal transaction (`apply_preview`).
"""

from __future__ import annotations

from typing import Any

from mkos_schemas.codes import ErrorCode

from app.core.logging import get_logger
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.adapter_input import build_adapter_listing
from app.services.remote_preview import apply_preview, previewable
from app.services.repair_input import adapter_repairs
from app.workers.errors import JobFailed, RetryableJobError
from app.workers.payloads import StatusSyncPayload
from app.workers.previewers import PreviewUnsupported, open_previewer
from app.workers.readers import AccountInactive, ReadFailed
from app.workers.registry import JobContext, Outcome
from app.workers.status_sync import auth_expired_report

log = get_logger(__name__)


async def run_remote_preview(ctx: JobContext) -> Outcome:
    payload = StatusSyncPayload.model_validate(ctx.payload)
    version_id = payload.version_id
    async with tenant_uow(ctx.tenant_id) as uow:
        listing = await uow.listings.get(payload.listing_id)
        if (
            listing is None
            or version_id is None
            or listing.current_version_id != version_id
            or not previewable(listing, payload.trigger)
        ):
            return Outcome("succeeded", result={"skipped": "stale"})
        account = await uow.marketplace_accounts.active_for_publish(
            listing.marketplace, listing.marketplace_account_id
        )
        if account is None:
            return Outcome("succeeded", result={"skipped": "account_not_active"})
        source = await uow.listings.preview_source(listing.id)
        family = await uow.product_facts.resolve_family(listing.product_id)
        if source is None or source.version is None or family is None:
            return Outcome("succeeded", result={"skipped": "stale"})
        skus = {row.id: row.seller_sku for row in await uow.listing_items.for_listing(listing.id)}
        repairs = adapter_repairs(await uow.listing_repairs.live_for_listing(listing.id), skus)
        item = build_adapter_listing(
            source,
            facts=family,
            image_refs={},
            idempotency_key=f"preview:{ctx.job_run_id}",
            repairs=repairs,
        )
    try:
        view = await open_previewer(ctx.tenant_id, account)
    except PreviewUnsupported:
        return Outcome("succeeded", result={"skipped": "unsupported"})
    except AccountInactive:
        raise JobFailed(auth_expired_report()) from None
    except ReadFailed:
        raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE) from None
    try:
        verdict = await view.preview_remote(item)
    except ReadFailed:
        raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE) from None
    finally:
        await view.aclose()
    result: dict[str, Any] = {
        "remote_check": verdict.status,
        "items": len(verdict.errors),
        "account_id": str(account),
    }
    if verdict.applied:
        result["preview_applied"] = True

    async def persist(uow: TenantUnitOfWork) -> None:
        result.update(
            await apply_preview(
                uow, ctx.job_run_id, payload.listing_id, version_id, verdict, payload.trigger
            )
        )

    log.info("preview.read", job_run_id=str(ctx.job_run_id), status=verdict.status)
    return Outcome("succeeded", result=result, persist=persist)
