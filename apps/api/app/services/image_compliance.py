"""The `image_compliance` job body (S17b T7; PLAN D5, D6; DATABASE.md:484).

`run_image_compliance(ctx)`:

1. A short unit of work: the live image row (gone → `succeeded` with `skipped: image_gone`),
   its product's category, and per marketplace the resolved `images` rules (`load_rules`;
   `RuleLoadError` → `failed`).
2. The row's key must parse as an image key under this job's tenant, else `failed` and the
   object is never read (a key outside the tenant prefix: S13 L8).
3. The object, with no transaction open, capped at `MAX_IMAGE_BYTES`. A transport failure is
   retried (then `dead_letter`, the row stays `pending`); a missing or oversized object fails.
4. The checks in the killable child (`image_runner`): a hostile image is a CORRUPT report,
   never a retry; a misconfigured rule fails the job.
5. Another short unit of work: one UPDATE writes the reports, the worst-of status, the meta
   columns and `content_hash` = sha256 of the bytes read.

No model client and no `agent_runs` row (invariant 7). Logs carry ids and the status only.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot, JobKind, Marketplace
from mkos_schemas.rules import Rule

from app.adapters.storage import (
    StorageNotFound,
    StorageObjectTooLarge,
    StorageUnavailable,
    get_storage,
    images_bucket,
)
from app.core.logging import get_logger
from app.repositories.uow import tenant_uow
from app.services import image_runner
from app.services.image_keys import parse_image_key
from app.services.rules_loader import RuleLoadError, load_rules
from app.workers.errors import JobFailed, RetryableJobError, system_report
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
MAX_IMAGE_BYTES: Final = 10_485_760


def _failed(message: str, ctx: JobContext) -> JobFailed:
    return JobFailed(
        system_report(
            ErrorCode.INTERNAL_ERROR,
            message,
            attempt=ctx.attempt,
            max_attempts=POLICIES[JobKind.IMAGE_COMPLIANCE].max_attempts,
            request_id=ctx.request_id,
        )
    )


def _image_id(ctx: JobContext) -> UUID:
    try:
        return UUID(str(ctx.payload["image_id"]))
    except (KeyError, ValueError):
        raise _failed("the image job has no image id", ctx) from None


async def run_image_compliance(ctx: JobContext) -> Outcome:
    image_id = _image_id(ctx)
    rules: dict[Marketplace, list[Rule]] = {}
    async with tenant_uow(ctx.tenant_id) as uow:
        row = await uow.images.get_live(image_id)
        product = None if row is None else await uow.products.get_by_id(row.product_id)
        if row is None or product is None:
            return Outcome("succeeded", {"skipped": "image_gone", "image_id": str(image_id)})
        key, slot = row.storage_key, ImageSlot(row.slot)
        for market in Marketplace:
            try:
                loaded = await load_rules(
                    uow, tenant_id=ctx.tenant_id, marketplace=market, category=product.category
                )
            except RuleLoadError:
                raise _failed("the image rules cannot be loaded", ctx) from None
            rules[market] = [r for r in loaded.ruleset.rules if r.field == "images"]

    parsed = parse_image_key(key)
    if parsed is None or parsed.tenant_id != ctx.tenant_id:
        raise _failed("image key outside the tenant prefix", ctx)

    try:
        data = await get_storage().get(images_bucket(), key, max_bytes=MAX_IMAGE_BYTES)
    except StorageUnavailable:
        raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None
    except StorageNotFound:
        raise _failed("the image object is missing", ctx) from None
    except StorageObjectTooLarge:
        raise _failed("the image object is over the size cap", ctx) from None

    outcome = await image_runner.run_checks_isolated(
        data, image_id=image_id, slot=slot, rules=rules, now=datetime.now(UTC)
    )

    async with tenant_uow(ctx.tenant_id) as uow:
        status = await uow.images.write_report(
            image_id,
            reports=outcome.reports,
            meta=outcome.meta,
            content_hash=hashlib.sha256(data).hexdigest(),
        )
    if status is None:
        return Outcome("succeeded", {"skipped": "image_gone", "image_id": str(image_id)})
    log.info("image.checked", image_id=str(image_id), compliance_status=status)
    return Outcome("succeeded", {"image_id": str(image_id), "compliance_status": status})
