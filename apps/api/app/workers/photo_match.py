"""The variant photo match job body (V1.1, client feedback item 9): `run_photo_match(ctx)`.

A photo match is an `image_compliance` job (`entity_type='photo_match'`, entity = the image) whose
payload says `photo_match` (`app.workers.image_jobs` dispatches). The upload checks queue it
(`app.services.photo_match.queue_photo_match`) once a product photo passed its technical checks.

1. `PHOTO_MATCH_ENABLED` off: `skipped`, no reservation, no call; the stored check is marked
   `failed` with `retry` (not checked; a later upload check may queue it again).
2. One short read under the job's tenant: the live image whose stored match still names this
   job's key (else `superseded`: other pixels, another colour or a newer check), its product's
   title and category, and the reference photo: the product's main photo every colour shares,
   when it is not the photo itself.
3. The vision copies load with no transaction open.
4. Reserve the check (`PHOTO_MATCH_RESERVATION_USD`, the brand and platform daily caps and the AI
   credit); a refusal ends `budget` with no call (not checked, `retry`).
5. One seam call under a hard deadline. No `photo_match` in the installed pipeline, or no vision
   model configured, ends `unavailable` (no call, nothing spent; not checked, `retry`).
6. Every returned `AgentRunRecord` is checked and restamped from the job (SI7's `trust_records`)
   and stored in its own short unit first (invariant 7). An untrusted answer stores the check as
   `failed` and fails the job.
7. In the terminal transaction (`persist`): the verdict, the two scores, the colour seen, the
   reason, `checked_at` and (on `mismatch` or `unsure`) the seller's question are written to
   `metadata.photo_match`, fenced on the clean hash, the key and this job. A failed model call
   stores `failed` (not checked).

A timeout or a seam exception is retried by the job policy; a check that never stores reads as
not checked after `CHECKING_STALE`. Log lines carry ids, the verdict and the scores only, never
pixels, the title or the reason.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from mkos_schemas.jobs import ContextImage
from pydantic import ValidationError

from app.adapters.storage import get_storage
from app.core.logging import get_logger
from app.repositories.images import ListingImageRow
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.generation_images import load_context_images
from app.services.image_edit_result import trust_records
from app.services.photo_match import ASKS, question_for
from app.workers.errors import JobFailed, RetryableJobError, system_report
from app.workers.photo_match_seam import PhotoMatchAnswer, PhotoMatchRequest
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
# the hard deadline over the seam call: the credentials' timeout plus this grace
SEAM_GRACE_S: Final = 15
PROMPT_PREFIX: Final = "photo_match/"


@dataclass(frozen=True, slots=True)
class _Read:
    image: ListingImageRow
    reference: ListingImageRow | None
    product_id: uuid.UUID
    title: str
    category: str | None
    colour: str | None
    source_hash: str
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


def _payload(ctx: JobContext) -> tuple[uuid.UUID, str]:
    try:
        image_id = uuid.UUID(str(ctx.payload["image_id"]))
        key = str(ctx.payload["match_key"])
    except (KeyError, ValueError):
        raise _failed("the photo match job has no image id or key", ctx) from None
    if len(key) != 64:
        raise _failed("the photo match job has no image id or key", ctx)
    return image_id, key


def _done(image_id: uuid.UUID, status: str, **extra: Any) -> Outcome:
    return Outcome(
        "succeeded", {"image_id": str(image_id), "status": status, **extra}, zero_reservation=True
    )


async def _read(ctx: JobContext, image_id: uuid.UUID, key: str) -> _Read | str:
    """What the check needs, under the job's tenant; or why it ends now."""
    async with tenant_uow(ctx.tenant_id) as uow:
        image = await uow.images.get_live(image_id)
        stored = (image.metadata_ or {}).get("photo_match") if image is not None else None
        if image is None or not isinstance(stored, dict) or stored.get("key") != key:
            return "superseded"
        if stored.get("job_id") != str(ctx.job_run_id) or image.clean_content_hash is None:
            return "superseded"
        if stored.get("source_hash") != image.clean_content_hash:  # other pixels since the queue
            return "superseded"
        product = await uow.products.get_by_id(image.product_id)
        if product is None:
            return "superseded"
        rows = {row.image_id: row for row in await uow.images.listing_set(product.id)}
        own = rows.get(image.id)
        if own is None:
            return "superseded"
        shared_main = next(
            (
                row
                for row in rows.values()
                if row.slot == "main" and row.colourway_id is None and row.image_id != image.id
                and row.clean_content_hash is not None
            ),
            None,
        )  # fmt: skip
        job = await uow.job_runs.get_by_id(ctx.job_run_id)
        colour = stored.get("colour")
        return _Read(
            image=own,
            reference=shared_main,
            product_id=product.id,
            title=product.title,
            category=product.category,
            colour=colour if isinstance(colour, str) and colour else None,
            source_hash=image.clean_content_hash,
            claim_started_at=(job.started_at if job else None) or datetime.now(UTC),
        )


async def _settle(ctx: JobContext, ids: tuple[uuid.UUID, str], value: dict[str, Any]) -> None:
    """Store a check that ended with no verdict (`failed`; with `retry` when no call was made),
    fenced on this job, in its own short unit."""
    image_id, key = ids
    async with tenant_uow(ctx.tenant_id) as uow:
        image = await uow.images.get_live(image_id)
        if image is None or image.clean_content_hash is None:
            return
        stored = (image.metadata_ or {}).get("photo_match")
        if not isinstance(stored, dict):
            return
        await uow.images.write_photo_match(
            image_id,
            {**stored, "status": "failed", "checked_at": datetime.now(UTC).isoformat(), **value},
            source_hash=image.clean_content_hash,
            expect_key=key,
            expect_job=str(ctx.job_run_id),
        )


def _checked(ctx: JobContext, read: _Read, result: object) -> PhotoMatchAnswer | str:
    """The answer re-validated through JSON with its records trusted and restamped, or the name
    of the check it failed."""
    dump = getattr(result, "model_dump_json", None)
    if not callable(dump):
        return "type"
    try:
        checked = PhotoMatchAnswer.model_validate_json(dump())
    except (ValidationError, ValueError, TypeError):
        return "schema"
    if checked.status == "ok" and (checked.verdict is None or checked.product_match is None):
        return "verdict"
    records = trust_records(
        checked.agent_runs,
        prompt_prefix=PROMPT_PREFIX,
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
    ctx: JobContext, read: _Read, images: dict[uuid.UUID, ContextImage], credentials: Any
) -> object | None:
    from app.workers import photo_match_seam

    reference = images.get(read.reference.image_id) if read.reference is not None else None
    request = PhotoMatchRequest(
        tenant_id=ctx.tenant_id,
        product_id=read.product_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        image=images[read.image.image_id],
        reference=reference,
        title=" ".join(read.title.split())[:200] or None,
        category=read.category,
        colour=read.colour,
    )
    return await asyncio.wait_for(
        photo_match_seam.call_photo_match(request, credentials),
        timeout=credentials.timeout_seconds + SEAM_GRACE_S,
    )


def stored_value(answer: PhotoMatchAnswer, read: _Read, now: datetime) -> dict[str, Any]:
    """The fields of `metadata.photo_match` a finished check writes (beside its key, hash, colour
    and job)."""
    if answer.status != "ok" or answer.verdict is None:
        return {"status": "failed", "checked_at": now.isoformat()}
    return {
        "status": answer.verdict,
        "product_match": answer.product_match,
        "colour_match": answer.colour_match,
        "seen_colour": answer.seen_colour,
        "reason": answer.reason,
        "verdict_version": answer.verdict_version or None,
        "question": question_for(read.colour, read.title) if answer.verdict in ASKS else None,
        "checked_at": now.isoformat(),
    }


async def run_photo_match(ctx: JobContext) -> Outcome:
    from app.core.config import get_settings
    from app.services.pipeline_deps import SeamPortError, photo_facts_read_credentials
    from app.workers.budget import PHOTO_MATCH_RESERVATION_USD, BudgetRefused, reserve

    image_id, key = _payload(ctx)
    ids = (image_id, key)
    settings = get_settings()
    if not settings.photo_match_enabled:  # switched off after the upload: no call
        await _settle(ctx, ids, {"retry": True})
        return _done(image_id, "skipped")
    read = await _read(ctx, image_id, key)
    if isinstance(read, str):
        log.info("photo_match.ended", image_id=str(image_id), status=read)
        return _done(image_id, read)
    picked = [read.image] if read.reference is None else [read.reference, read.image]
    loaded = await load_context_images(get_storage(), ctx.tenant_id, picked)
    images = {image.image_id: image for image in loaded}
    if read.image.image_id not in images:  # the photo's vision copy is gone: nothing to check
        await _settle(ctx, ids, {"retry": True})
        return _done(image_id, "not_read")
    try:
        credentials = photo_facts_read_credentials(settings)  # the vision route
    except SeamPortError:
        await _settle(ctx, ids, {"retry": True})
        return _done(image_id, "unavailable")
    try:
        reserved = await reserve(
            ctx.tenant_id, ctx.job_run_id, ctx.attempt, amount=PHOTO_MATCH_RESERVATION_USD
        )
    except BudgetRefused as refused:
        log.info("photo_match.budget", image_id=str(image_id), scope=refused.scope)
        await _settle(ctx, ids, {"retry": True})
        return _done(image_id, "budget")
    if not reserved:  # the fence moved: a newer attempt owns the row
        raise _failed("the photo match job lost its claim", ctx)
    try:
        result = await _call(ctx, read, images, credentials)
    except Exception as error:  # a timeout or a pipeline bug: the job policy retries
        log.info(
            "photo_match.seam_failed",
            image_id=str(image_id),
            job_run_id=str(ctx.job_run_id),
            error=type(error).__name__,
        )
        raise RetryableJobError(ErrorCode.JOB_TIMEOUT) from None
    if result is None:  # the installed pipeline has no photo_match: no call was made
        await _settle(ctx, ids, {"retry": True})
        return _done(image_id, "unavailable")
    checked = _checked(ctx, read, result)
    if isinstance(checked, str):
        log.info(
            "photo_match.untrusted",
            image_id=str(image_id),
            job_run_id=str(ctx.job_run_id),
            check=checked,
        )
        await _settle(ctx, ids, {})
        raise _failed("the photo match answer was not trusted", ctx)
    async with tenant_uow(ctx.tenant_id) as uow:  # invariant 7: the records first
        for record in checked.agent_runs:
            await uow.agent_runs.insert(record)
    value = stored_value(checked, read, datetime.now(UTC))
    log.info(
        "photo_match.checked",
        image_id=str(image_id),
        job_run_id=str(ctx.job_run_id),
        verdict=value["status"],
        product_match=checked.product_match,
        colour_match=checked.colour_match,
        reference=read.reference is not None,
    )
    result_json: dict[str, Any] = {"image_id": str(image_id), "status": value["status"]}

    async def persist(uow: TenantUnitOfWork) -> None:
        image = await uow.images.get_live(image_id)
        stored = (image.metadata_ or {}).get("photo_match") if image is not None else None
        if not isinstance(stored, dict):
            result_json["status"] = "superseded"
            return
        written = await uow.images.write_photo_match(
            image_id,
            {
                key_: stored.get(key_)
                for key_ in ("key", "source_hash", "colour", "job_id", "queued_at")
            }
            | value,
            source_hash=read.source_hash,
            expect_key=key,
            expect_job=str(ctx.job_run_id),
        )
        if not written:  # other pixels, another colour or a newer check
            result_json["status"] = "superseded"

    return Outcome("succeeded", result_json, persist=persist, zero_reservation=True)
