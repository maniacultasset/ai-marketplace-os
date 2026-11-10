"""The photo read on upload job body (V1.1): `run_photo_facts_read(ctx)`.

A photo read on upload is an `image_compliance` job (`entity_type='photo_facts_read'`, entity =
the product) whose payload says `photo_facts_read` (`app.workers.image_jobs` dispatches). The
upload checks queue it (`app.services.photo_read_upload`) when a front, back or close-up photo of
a style or standalone product has its vision copy. It is the generation's SI3 photo read on its
own: the same targets (`build_targets`), the same screening (`screen`) and the same guarded store
(`apply_photo_decision`), so a value read here is an unconfirmed `ai` fact or a question, never a
seller fact, and never a critical key.

1. `PHOTO_READ_ON_UPLOAD_ENABLED` off: `skipped`, no reservation, no call.
2. One short read under the job's tenant: the product (a variant or a gone product is `skipped`),
   its rules template (the marketplace of its first listing, Amazon when it has none), the facts,
   the photos with a vision copy, the newest recorded photo set and the rejected photo values.
3. The vision copies load with no transaction open. The photo set is computed as generation
   computes it; when it equals the newest recorded set (a generation or an earlier upload read
   already read these photos) or nothing is left to read, the job ends `unchanged` or
   `not_read` with no reservation and no call. So one photo set is paid for once.
4. Reserve the read (`PHOTO_READ_RESERVATION_USD`, the brand and platform daily caps and the AI
   credit); a refusal ends `budget` with no call and records no set, so the next generation still
   reads the photos.
5. One seam call under a hard deadline. No `read_photo_facts` in the installed pipeline, or no
   vision model configured, ends `unavailable` (no call, nothing spent).
6. Every returned `AgentRunRecord` is checked and restamped from the job (SI7's `trust_records`)
   and stored in its own short unit first (invariant 7). An untrusted answer stores nothing and
   fails the job.
7. In the terminal transaction (`persist`): `apply_photo_decision` under the product lock (the
   superseded check, the guarded writes and questions, their audit rows). The job's result
   records `photo_set` and `photo_read` exactly as a generation does, so the product read shows
   it (`latest_photo_read`) and the next generation skips the same photos.

A timeout or a seam exception is retried by the job policy. Log lines carry ids, counts and
statuses only, never pixels or values.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot, JobKind, Marketplace
from mkos_schemas.jobs import CategoryTemplate, ContextImage, PhotoTarget
from mkos_schemas.photo_facts_read import (
    MAX_PHOTO_READ_IMAGES,
    PhotoFactsReadRequest,
    PhotoFactsReadResult,
)
from pydantic import ValidationError

from app.adapters.storage import get_storage
from app.core.logging import get_logger
from app.core.resolved_facts import ProductFacts
from app.repositories.images import ListingImageRow
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.generation_images import load_context_images, pick_context
from app.services.image_edit_result import trust_records
from app.services.photo_facts import (
    PHOTO_PROMPT_PREFIX,
    PHOTO_SLOTS,
    PhotoSet,
    build_targets,
    photo_read_skip,
    photo_set,
    screen,
)
from app.services.photo_facts_store import apply_photo_decision
from app.services.rules_loader import RuleLoadError, load_rules
from app.workers.errors import JobFailed, RetryableJobError, system_report
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
# the hard deadline over the seam call: the credentials' timeout plus this grace
SEAM_GRACE_S: Final = 15
# The order photos are sent in, as the pipeline's photo read orders them (SI12): front, back,
# close-up, any other secondary, then lifestyle.
_ORDER: Final = (
    ("main", None),
    ("secondary", "back"),
    ("secondary", "close_up"),
    ("secondary", None),
    ("lifestyle", None),
)


@dataclass(frozen=True, slots=True)
class _Read:
    product_id: uuid.UUID
    category: str
    template: CategoryTemplate
    facts: ProductFacts
    rows: tuple[ListingImageRow, ...]
    seen_set: PhotoSet | None
    rejections: dict[str, str]
    claim_started_at: datetime


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


def _product_id(ctx: JobContext) -> uuid.UUID:
    try:
        return uuid.UUID(str(ctx.payload["product_id"]))
    except (KeyError, ValueError):
        raise _failed("the photo read job has no product id", ctx) from None


def _done(product_id: uuid.UUID, status: str, **extra: Any) -> Outcome:
    return Outcome(
        "succeeded",
        {"product_id": str(product_id), "status": status, **extra},
        zero_reservation=True,
    )


def ordered_photos(images: list[ContextImage]) -> list[ContextImage]:
    """The product photos to send, front first (`_ORDER`, context order within a rank), at most
    `MAX_PHOTO_READ_IMAGES`; a label or a size chart is never sent."""

    def rank(image: ContextImage) -> int:
        slot = ImageSlot(image.slot).value
        role = image.role if slot == "secondary" else None
        return _ORDER.index((slot, role))

    photos = [image for image in images if image.slot in PHOTO_SLOTS]
    return sorted(photos, key=rank)[:MAX_PHOTO_READ_IMAGES]


async def _marketplace(uow: TenantUnitOfWork, product_id: uuid.UUID) -> Marketplace:
    """The marketplace whose template spells the values: the product's first listing (Amazon
    first), else Amazon."""
    markets = {str(listing.marketplace) for listing in await uow.listings.for_product(product_id)}
    for market in Marketplace:
        if market.value in markets:
            return market
    return Marketplace.AMAZON


async def _read(ctx: JobContext, product_id: uuid.UUID) -> _Read | str:
    """What the read needs, under the job's tenant; or why it ends now."""
    async with tenant_uow(ctx.tenant_id) as uow:
        product = await uow.products.get_by_id(product_id)
        if product is None or product.parent_product_id is not None:
            return "skipped"
        if product.category is None:
            return "not_read"
        market = await _marketplace(uow, product.id)
        try:
            rules = await load_rules(
                uow, tenant_id=ctx.tenant_id, marketplace=market, category=product.category
            )
        except RuleLoadError:
            return "not_read"
        if rules.template is None:
            return "not_read"
        family = await uow.product_facts.resolve_family(product.id)
        if family is None:
            return "skipped"
        job = await uow.job_runs.get_by_id(ctx.job_run_id)
        return _Read(
            product_id=product.id,
            category=product.category,
            template=rules.template,
            facts=family[0],
            rows=tuple(await uow.images.listing_set(product.id)),
            seen_set=await uow.job_runs.latest_photo_set(product.id),
            rejections=await uow.products.photo_rejections(product.id),
            claim_started_at=(job.started_at if job else None) or datetime.now(UTC),
        )


def _checked(ctx: JobContext, read: _Read, result: object) -> PhotoFactsReadResult | str:
    """The answer re-validated through JSON with its records trusted and restamped, or the name
    of the check it failed."""
    dump = getattr(result, "model_dump_json", None)
    if not callable(dump):
        return "type"
    try:
        checked = PhotoFactsReadResult.model_validate_json(dump())
    except (ValidationError, ValueError, TypeError):
        return "schema"
    records = trust_records(
        checked.agent_runs,
        prompt_prefix=PHOTO_PROMPT_PREFIX,
        tenant_id=ctx.tenant_id,
        product_id=read.product_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        attempt=min(max(ctx.attempt, 1), 3),
        claim_started_at=read.claim_started_at,
        now=datetime.now(UTC),
        triggers=frozenset({"vision"}),
    )
    if isinstance(records, str):
        return records
    return checked.model_copy(update={"agent_runs": list(records)})


async def _call(
    ctx: JobContext,
    read: _Read,
    photos: list[ContextImage],
    targets: list[PhotoTarget],
    credentials: Any,
) -> object | None:
    from app.workers import photo_facts_seam

    request = PhotoFactsReadRequest(
        tenant_id=ctx.tenant_id,
        product_id=read.product_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        attempt=min(max(ctx.attempt, 1), 3),
        images=photos,
        targets=targets,
        category=read.category,
    )
    return await asyncio.wait_for(
        photo_facts_seam.call_read_photo_facts(request, credentials),
        timeout=credentials.timeout_seconds + SEAM_GRACE_S,
    )


async def run_photo_facts_read(ctx: JobContext) -> Outcome:
    from app.core.config import get_settings
    from app.services.pipeline_deps import SeamPortError, photo_facts_read_credentials
    from app.workers.budget import PHOTO_READ_RESERVATION_USD, BudgetRefused, reserve

    product_id = _product_id(ctx)
    settings = get_settings()
    if not settings.photo_read_on_upload_enabled:  # switched off after the upload: no call
        return _done(product_id, "skipped")
    read = await _read(ctx, product_id)
    if isinstance(read, str):
        log.info("photo_read.ended", product_id=str(product_id), status=read)
        return _done(product_id, read)
    picked = pick_context(read.rows)
    context = await load_context_images(get_storage(), ctx.tenant_id, picked)
    hashes = {r.image_id: r.clean_content_hash or "" for r in picked}
    current = photo_set(context, hashes)
    skip = photo_read_skip(
        read.facts, read.template, context, current_set=current, last_set=read.seen_set
    )
    targets = build_targets(
        read.facts, read.template, context, current_set=current, last_set=read.seen_set
    )
    if skip is not None or not targets:  # read already, or nothing to read: no spend
        status = skip or "not_read"
        log.info("photo_read.ended", product_id=str(product_id), status=status)
        return _done(product_id, status)
    try:
        credentials = photo_facts_read_credentials(settings)
    except SeamPortError:
        return _done(product_id, "unavailable")
    try:
        reserved = await reserve(
            ctx.tenant_id, ctx.job_run_id, ctx.attempt, amount=PHOTO_READ_RESERVATION_USD
        )
    except BudgetRefused as refused:
        log.info("photo_read.budget", product_id=str(product_id), scope=refused.scope)
        return _done(product_id, "budget")
    if not reserved:  # the fence moved: a newer attempt owns the row
        raise _failed("the photo read job lost its claim", ctx)
    photos = ordered_photos(context)
    try:
        result = await _call(ctx, read, photos, targets, credentials)
    except Exception as error:  # a timeout or a pipeline bug: the job policy retries
        log.info(
            "photo_read.seam_failed",
            product_id=str(product_id),
            job_run_id=str(ctx.job_run_id),
            error=type(error).__name__,
        )
        raise RetryableJobError(ErrorCode.JOB_TIMEOUT) from None
    if result is None:  # the installed pipeline has no read_photo_facts: no call was made
        return _done(product_id, "unavailable")
    checked = _checked(ctx, read, result)
    if isinstance(checked, str):
        log.info(
            "photo_read.untrusted",
            product_id=str(product_id),
            job_run_id=str(ctx.job_run_id),
            check=checked,
        )
        raise _failed("the photo read answer was not trusted", ctx)
    async with tenant_uow(ctx.tenant_id) as uow:  # invariant 7: the records first
        for record in checked.agent_runs:
            await uow.agent_runs.insert(record)
    decision = screen(
        checked.observations,
        checked.agent_runs,
        targets=targets,
        images=photos,
        facts=read.facts,
        required=frozenset(read.template.required_attributes),
        rejected=read.rejections,
        template=read.template,
    )
    log.info(
        "photo_read.screened",
        product_id=str(product_id),
        job_run_id=str(ctx.job_run_id),
        writes=len(decision.writes),
        tasks=len(decision.tasks),
        dropped=[reason for _key, reason in decision.dropped],
    )
    result_json: dict[str, Any] = {"product_id": str(product_id)}
    if decision.run_id is None:  # the call failed: nothing applied, no set recorded
        result_json.update(
            status="failed", photo_read={"status": "failed", "written": 0, "asked": 0}
        )
        return Outcome("succeeded", result_json, zero_reservation=True)

    async def persist(uow: TenantUnitOfWork) -> None:
        applied = await apply_photo_decision(
            uow,
            job_id=ctx.job_run_id,
            product_id=product_id,
            decision=decision,
            pre_facts=read.facts,
            seen_set=read.seen_set,
            sent_set=current,
            now=datetime.now(UTC),
        )
        if applied.record_set is None:  # another photo read applied first
            result_json["status"] = "superseded"
            return
        result_json["status"] = "read"
        result_json["photo_set"] = [list(pair) for pair in applied.record_set]
        result_json["photo_read"] = {
            "status": "read",
            "written": len(applied.written),
            "asked": applied.tasks,
        }
        log.info(
            "photo_read.applied",
            product_id=str(product_id),
            job_run_id=str(ctx.job_run_id),
            written=len(applied.written),
            asked=applied.tasks,
        )

    return Outcome("succeeded", result_json, persist=persist, zero_reservation=True)
