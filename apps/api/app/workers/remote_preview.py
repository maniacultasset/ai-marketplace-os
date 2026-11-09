"""The dry-run body of `listing_status_sync` (`op = "preview"`; SI11 T7; ADR-0028 proposed).

1. One short read transaction: the listing, its current version's adapter input (no images,
   CP-D23: a preview never makes or sends a public image copy) and the live repairs. A version
   that is no longer current, a listing no longer pre-approval, or an account that is not
   `active` ends the job `{"skipped": ...}` with no marketplace call.
2. Outside any transaction: `PreviewView.preview_remote` (the only marketplace write-shaped call
   this body can make, and it saves nothing; `.importlinter` `remote-preview-preview-only`).
   SI13: with both `AMAZON_VALIDATION_PREVIEW_ENABLED` and `AMAZON_CATALOG_PRECHECK_ENABLED` on
   (CP-D6), `PreviewView.precheck` then searches the catalog by barcode and reads the matched
   ASINs' brand restrictions; its items join the verdict's (an ERROR blocks approval through the
   same gate) and `result_json.precheck` records `{checked, matched: [asin...], failed}`. A failed
   pre-check never blocks: the verdict stays the preview's. A failed call raises `RetryableJobError`
   (the job's retry; a dead letter marks the check `failed`), an account that refuses to open
   fails the job with AUTH_EXPIRED.
3. The writes run in the wrapper's fenced terminal transaction (`apply_preview`).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Severity
from mkos_schemas.errors import MAX_ERROR_ITEMS

from app.adapters.base import PrecheckResult, RemotePreview
from app.core.config import get_settings
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
        marketplace = None if listing is None else listing.marketplace
        if (
            listing is None
            or version_id is None
            or listing.current_version_id != version_id
            # a queued job runs wherever its verdict may still be written (`apply_preview`):
            # a validate re-check (fix round 2) is queued from `blocked/validation_errors`
            or not previewable(listing, payload.trigger, recheck=True)
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
    checked: PrecheckResult | None = None
    try:
        verdict = await view.preview_remote(item)
        if precheck_on(marketplace) and verdict.status != "failed":
            checked = await view.precheck(item, restrictions=True)
            verdict = with_precheck(verdict, checked)
    except ReadFailed:
        raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE) from None
    finally:
        await view.aclose()
    result: dict[str, Any] = {
        "remote_check": verdict.status,
        "items": len(verdict.errors),
        "account_id": str(account),
    }
    if checked is not None:
        result["precheck"] = {
            "checked": checked.checked,
            "matched": sorted({asin for _, asin in checked.matched})[:20],
            "failed": len(checked.failed),
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


def precheck_on(marketplace: str | None) -> bool:
    """CP-D6: the pre-check runs only inside the Amazon dry run, with both switches on."""
    settings = get_settings()
    return (
        marketplace == "amazon"
        and settings.amazon_validation_preview_enabled
        and settings.amazon_catalog_precheck_enabled
    )


def with_precheck(verdict: RemotePreview, checked: PrecheckResult) -> RemotePreview:
    """The verdict with the pre-check's items and checked paths joined: an ERROR among them
    turns a `clean` dry run into `issues`; nothing else changes."""
    if not checked.items and not checked.scope:
        return verdict
    errors = (*verdict.errors, *checked.items)[:MAX_ERROR_ITEMS]
    status = verdict.status
    if status == "clean" and any(e.severity is Severity.ERROR for e in checked.items):
        status = "issues"
    scope = tuple(sorted({*verdict.scope, *checked.scope}))
    return dataclasses.replace(verdict, status=status, errors=errors, scope=scope)
