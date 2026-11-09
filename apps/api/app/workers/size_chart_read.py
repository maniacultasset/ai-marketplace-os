"""The size-chart read job body (SI12 B3): `run_size_chart_read(ctx)`.

A size-chart read is an `image_compliance` job (`entity_type='size_chart_read'`, entity = the
image) whose payload says `size_chart_read` (`app.workers.image_jobs` dispatches). It reads the
seller's size-chart photo into a suggestion and never writes a fact, an attribute, provenance or a
listing: the only write is `images.metadata.size_chart_read` (fenced on the clean hash).

1. One short read under the job's tenant: the image is still the product's live size chart (a
   style or standalone) with a clean copy and its vision size, else `skipped` with no call.
2. Reserve the read (`SIZE_CHART_READ_RESERVATION_USD`); a refusal stores `failed`.
3. GET `vision.jpg`; one seam call under a hard deadline. No `read_size_chart` in the installed
   pipeline, no vision model configured, or `SIZE_CHART_READ_ENABLED` off, stores `unavailable`
   (no call, nothing spent).
4. Every returned `AgentRunRecord` is checked and restamped from the job (SI7's `trust_records`)
   and stored in its own short unit first (invariant 7). An untrusted answer stores nothing and
   fails the job (the terminal hook stores `failed`).
5. The chart is normalised with the seller size-chart bounds: `read` with the chart, or
   `unreadable` (no chart, `unreadable`, or a chart outside the bounds). An answer carrying an
   error and no chart is `failed`, so the seller can ask again. A unit the pipeline could not back
   with a printed line stays `unknown`: the seller picks `cm` or `in` before saving. A read `cm` or
   `in` keeps its printed line (`unit_source_line`) for the seller to confirm.

A timeout or a seam exception is retried by the job policy; after the last attempt the terminal
hook stores `failed`. Log lines carry ids and statuses only, never pixels or chart text.
"""

from __future__ import annotations

import asyncio
import base64
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot, JobKind
from mkos_schemas.jobs import ContextImage
from mkos_schemas.size_chart_read import (
    MAX_SIZE_CHART_UNIT_LINE_CHARS,
    SizeChartReadRequest,
    SizeChartReadResult,
)
from pydantic import ValidationError

from app.adapters.storage import (
    StorageNotFound,
    StorageObjectTooLarge,
    StorageUnavailable,
    get_storage,
    images_bucket,
)
from app.core.logging import get_logger
from app.repositories.uow import tenant_uow
from app.services.image_edit_result import trust_records
from app.services.image_keys import derived_key
from app.services.intake_values import SizeChartError, normalize_size_chart
from app.workers.errors import JobFailed, RetryableJobError, system_report
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
PROMPT_PREFIX: Final = "size_chart_read/"
MAX_VISION_BYTES: Final = 2 * 1024 * 1024
# the hard deadline over the seam call: the credentials' timeout plus this grace
SEAM_GRACE_S: Final = 15


@dataclass(frozen=True, slots=True)
class _Inputs:
    image_id: UUID
    product_id: UUID
    category: str
    source_hash: str
    width: int
    height: int
    claim_started_at: datetime


def _failed(message: str, ctx: JobContext, code: ErrorCode = ErrorCode.INTERNAL_ERROR) -> JobFailed:
    return JobFailed(
        system_report(
            code,
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
        raise _failed("the size-chart read job has no image id", ctx) from None


def _done(image_id: UUID, status: str) -> Outcome:
    return Outcome(
        "succeeded", {"image_id": str(image_id), "status": status}, zero_reservation=True
    )


def _vision_size(metadata: Any) -> tuple[int, int] | None:
    vision = metadata.get("vision") if isinstance(metadata, dict) else None
    if not isinstance(vision, dict):
        return None
    w, h = vision.get("w"), vision.get("h")
    if isinstance(w, int) and isinstance(h, int) and not isinstance(w, bool | None):
        return w, h
    return None


async def _inputs(ctx: JobContext, image_id: UUID) -> _Inputs | None:
    """The image as it is now, under the job's tenant: still the product's live size chart."""
    async with tenant_uow(ctx.tenant_id) as uow:
        image = await uow.images.get_live(image_id)
        if image is None or image.slot != ImageSlot.SIZE_CHART.value:
            return None
        product = await uow.products.get_by_id(image.product_id)
        if product is None or product.parent_product_id is not None:
            return None
        current = await uow.images.live_size_chart(product.id)
        size = _vision_size(image.metadata_)
        if current is None or current.id != image.id or image.clean_content_hash is None:
            return None
        if size is None:
            return None
        job = await uow.job_runs.get_by_id(ctx.job_run_id)
        return _Inputs(
            image_id=image.id,
            product_id=product.id,
            category=product.category,
            source_hash=image.clean_content_hash,
            width=size[0],
            height=size[1],
            claim_started_at=(job.started_at if job else None) or datetime.now(UTC),
        )


async def _store(ctx: JobContext, inputs: _Inputs, value: dict[str, Any]) -> bool:
    stored = {**value, "read_at": datetime.now(UTC).isoformat()}
    async with tenant_uow(ctx.tenant_id) as uow:
        written = await uow.images.write_size_chart_read(
            inputs.image_id, stored, source_hash=inputs.source_hash, job_id=ctx.job_run_id
        )
    log.info(
        "size_chart_read.stored",
        image_id=str(inputs.image_id),
        job_run_id=str(ctx.job_run_id),
        status=value["status"],
        written=written,
    )
    return written


async def _settle(ctx: JobContext, inputs: _Inputs, value: dict[str, Any]) -> Outcome:
    written = await _store(ctx, inputs, value)
    return _done(inputs.image_id, value["status"] if written else "skipped")


async def _vision(ctx: JobContext, inputs: _Inputs) -> bytes | None:
    key = derived_key(ctx.tenant_id, inputs.product_id, inputs.image_id, "vision.jpg")
    try:
        return await get_storage().get(images_bucket(), key, max_bytes=MAX_VISION_BYTES)
    except (StorageNotFound, StorageObjectTooLarge):
        return None
    except StorageUnavailable:
        raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None


async def _call(
    ctx: JobContext, inputs: _Inputs, data: bytes, credentials: Any
) -> SizeChartReadResult | None:
    from app.workers import size_chart_seam

    request = SizeChartReadRequest(
        tenant_id=ctx.tenant_id,
        product_id=inputs.product_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        attempt=min(max(ctx.attempt, 1), 3),
        image=ContextImage(
            image_id=inputs.image_id,
            slot=ImageSlot.SIZE_CHART,
            width=inputs.width,
            height=inputs.height,
            data_b64=base64.b64encode(data).decode("ascii"),
        ),
        category=inputs.category,
    )
    return await asyncio.wait_for(
        size_chart_seam.call_read_size_chart(request, credentials),
        timeout=credentials.timeout_seconds + SEAM_GRACE_S,
    )


def _checked(ctx: JobContext, inputs: _Inputs, result: object) -> SizeChartReadResult | str:
    """The answer re-validated through JSON with its records trusted and restamped, or the name
    of the check it failed."""
    if not isinstance(result, SizeChartReadResult):
        return "type"
    try:
        checked = SizeChartReadResult.model_validate_json(result.model_dump_json())
    except (ValidationError, ValueError, TypeError):
        return "schema"
    records = trust_records(
        checked.agent_runs,
        prompt_prefix=PROMPT_PREFIX,
        tenant_id=ctx.tenant_id,
        product_id=inputs.product_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        attempt=min(max(ctx.attempt, 1), 3),
        claim_started_at=inputs.claim_started_at,
        now=datetime.now(UTC),
    )
    if isinstance(records, str):
        return records
    return checked.model_copy(update={"agent_runs": list(records)})


def _source_line(unit: str, line: str | None) -> str | None:
    """The printed line behind a read `cm` or `in`, NFC with whitespace collapsed; None for an
    `unknown` unit, or a line that is blank, too long or holds a control or format character."""
    if unit == "unknown" or line is None:
        return None
    text = " ".join(unicodedata.normalize("NFC", line).split())
    if not text or len(text) > MAX_SIZE_CHART_UNIT_LINE_CHARS:
        return None
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        return None
    return text


def _verdict(result: SizeChartReadResult) -> dict[str, Any]:
    if result.chart is None and result.error is not None and not result.unreadable:
        return {"status": "failed"}
    if result.unreadable or result.chart is None:
        return {"status": "unreadable"}
    raw = result.chart.model_dump(mode="json", exclude={"unit_source_line"})
    try:
        chart = normalize_size_chart(raw, unknown_unit=True)
    except SizeChartError:
        return {"status": "unreadable"}
    return {
        "status": "read",
        "chart": chart,
        "unit_source_line": _source_line(chart["unit"], result.chart.unit_source_line),
        "confidence": result.confidence,
    }


async def run_size_chart_read(ctx: JobContext) -> Outcome:
    from app.core.config import get_settings
    from app.services.pipeline_deps import SeamPortError, size_chart_read_credentials
    from app.workers.budget import SIZE_CHART_READ_RESERVATION_USD, BudgetRefused, reserve

    image_id = _image_id(ctx)
    inputs = await _inputs(ctx, image_id)
    if inputs is None:
        log.info("size_chart_read.skipped", image_id=str(image_id), job_run_id=str(ctx.job_run_id))
        return _done(image_id, "skipped")
    settings = get_settings()
    if not settings.size_chart_read_enabled:  # switched off after the ask: no reservation, no call
        return await _settle(ctx, inputs, {"status": "unavailable"})
    try:
        credentials = size_chart_read_credentials(settings)
    except SeamPortError:
        return await _settle(ctx, inputs, {"status": "unavailable"})
    try:
        reserved = await reserve(
            ctx.tenant_id, ctx.job_run_id, ctx.attempt, amount=SIZE_CHART_READ_RESERVATION_USD
        )
    except BudgetRefused:
        return await _settle(ctx, inputs, {"status": "failed"})
    if not reserved:  # the fence moved: a newer attempt owns the row
        raise _failed("the size-chart read job lost its claim", ctx)
    data = await _vision(ctx, inputs)
    if data is None:
        return await _settle(ctx, inputs, {"status": "failed"})
    try:
        result = await _call(ctx, inputs, data, credentials)
    except Exception as error:  # a timeout or a pipeline bug: the job policy retries
        log.info(
            "size_chart_read.seam_failed",
            image_id=str(inputs.image_id),
            job_run_id=str(ctx.job_run_id),
            error=type(error).__name__,
        )
        raise RetryableJobError(ErrorCode.JOB_TIMEOUT) from None
    if result is None:  # the installed pipeline has no read_size_chart: no call was made
        return await _settle(ctx, inputs, {"status": "unavailable"})
    checked = _checked(ctx, inputs, result)
    if isinstance(checked, str):
        log.info(
            "size_chart_read.untrusted",
            image_id=str(inputs.image_id),
            job_run_id=str(ctx.job_run_id),
            check=checked,
        )
        raise _failed("the size-chart read answer was not trusted", ctx)
    async with tenant_uow(ctx.tenant_id) as uow:  # invariant 7: the records first
        for record in checked.agent_runs:
            await uow.agent_runs.insert(record)
    return await _settle(ctx, inputs, _verdict(checked))
