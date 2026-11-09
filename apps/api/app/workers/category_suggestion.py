"""The classification job body (SI14, AC5): `run_category_suggestion(ctx)`.

A classification is an `image_compliance` job (`entity_type='category_suggestion'`, entity = the
product) whose payload names `category_suggestion` (`app.workers.image_jobs` dispatches). It
stores a suggestion in `products.category_suggestion` and never writes the category, a fact,
provenance or a listing: the seller confirms a candidate with PATCH (AC6).

1. One short read: the product (gone or a variant: `skipped`, nothing stored), its classify text,
   up to 6 clean photos and the categories that may be offered (lamps only while
   `CATEGORY_LAMPS_ENABLED` is on), and their input digest.
2. Switched off, nothing to offer, no classify credentials, or no `classify_product` in the
   installed pipeline: `unavailable`, with no call and nothing spent.
3. Reserve `CLASSIFY_RESERVATION_USD` (a refusal stores `failed`), then one seam call under a hard
   deadline.
4. The answer is re-validated through JSON and every record is checked and restamped from the job
   (`trust_records`: agent `attribute`, trigger `vision` or `generate`, prompt `classify/`). An
   untrusted answer fails the job (the terminal hook stores `failed`). The records are stored in
   their own unit first (invariant 7).
5. Screening: a candidate outside the offer or repeated is dropped; the rationale is one line of
   printable text (control and format characters removed), at most 300 characters; only evidence
   refs that point at a sent input are kept; highest confidence first, at most 3.
6. Under the product row lock the digest is recomputed: when the text, the photos or the offer
   changed during the call the answer is stale and stores `failed`, with no task.
7. `suggested` is stored. When the product still has no category and the top confidence is below
   0.80 (or nothing fits), one `category_review` task opens (one live per product).

The seller's decision wins: a stored `confirmed` or `overridden` value is never replaced by a
`failed` or `unavailable` write, nor by a `suggested` one for the same inputs. When the seller
wrote another category after the ask (the payload's `category`) and before the answer, the answer
is stored already settled: `confirmed` when that category is a candidate, else `overridden`.

A timeout or a seam exception is retried by the job policy; after the last attempt the terminal
hook stores `failed`. Log lines carry ids, counts and statuses only.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from mkos_schemas.classify import (
    CLASSIFY_CONFIDENCE_TASK_THRESHOLD,
    MAX_CANDIDATES,
    CategoryOption,
    ClassifyRequest,
    ClassifyResult,
    ProductText,
)
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from pydantic import ValidationError

from app.adapters.storage import get_storage
from app.core.logging import get_logger
from app.repositories.products import SETTLED_SUGGESTION_STATUSES
from app.repositories.uow import tenant_uow
from app.services.category_suggest import SuggestInputs, suggest_inputs
from app.services.generation_images import load_context_images
from app.services.image_edit_result import trust_records
from app.workers.errors import JobFailed, RetryableJobError, system_report
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
PROMPT_PREFIX: Final = "classify/"
AGENT: Final = "attribute"
TRIGGERS: Final = frozenset({"generate", "vision"})
SEAM_GRACE_S: Final = 15
MAX_RATIONALE: Final = 300
MAX_REFS: Final = 12
MAX_REF_CHARS: Final = 200
TEXT_REFS: Final = frozenset({"title", "description", "brand", "seller_category"})
_IMAGE_REF: Final = re.compile(r"^images\[([0-9a-f-]{36})\]$")
REVIEW_REASON: Final = (
    "The suggested categories are not certain enough; pick the category that fits."
)


@dataclass(frozen=True, slots=True)
class _Job:
    product_id: UUID
    inputs: SuggestInputs
    has_category: bool
    claim_started_at: datetime
    # the category when the job was asked for; unknown on a job queued before it was recorded
    asked_known: bool = False
    asked_category: str | None = None


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


def _asked(ctx: JobContext) -> tuple[bool, str | None]:
    payload = ctx.payload.get("category_suggestion")
    if not isinstance(payload, dict) or "category" not in payload:
        return False, None
    category = payload["category"]
    return True, category if isinstance(category, str) else None


def _product_id(ctx: JobContext) -> UUID:
    payload = ctx.payload.get("category_suggestion")
    try:
        return UUID(str(payload["product_id"]))  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        raise _failed("the classification job has no product id", ctx) from None


def _done(product_id: UUID, status: str) -> Outcome:
    return Outcome(
        "succeeded", {"product_id": str(product_id), "status": status}, zero_reservation=True
    )


def _value(
    status: str, job: _Job, ctx: JobContext, result: ClassifyResult | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "candidates": (
            [] if result is None else [c.model_dump(mode="json") for c in result.candidates]
        ),
        "input_digest": job.inputs.digest,
        "calibration_version": None if result is None else result.calibration_version[:32],
        "job_id": str(ctx.job_run_id),
        "at": datetime.now(UTC).isoformat(),
    }


async def _read(ctx: JobContext, product_id: UUID) -> _Job | None:
    from app.core.config import get_settings

    async with tenant_uow(ctx.tenant_id) as uow:
        product = await uow.products.get_by_id(product_id)
        if product is None or product.parent_product_id is not None:
            return None
        inputs = await suggest_inputs(uow, product, get_settings())
        job = await uow.job_runs.get_by_id(ctx.job_run_id)
        asked_known, asked_category = _asked(ctx)
        return _Job(
            product_id=product.id,
            inputs=inputs,
            has_category=product.category is not None,
            claim_started_at=(job.started_at if job else None) or datetime.now(UTC),
            asked_known=asked_known,
            asked_category=asked_category,
        )


async def _settle(ctx: JobContext, job: _Job, value: dict[str, Any]) -> Outcome:
    """`failed` or `unavailable`, unless the seller already settled the suggestion."""
    async with tenant_uow(ctx.tenant_id) as uow:
        written = await uow.products.write_category_suggestion(
            job.product_id, value, keep_settled=True
        )
    log.info(
        "category_suggestion.stored",
        product_id=str(job.product_id),
        job_run_id=str(ctx.job_run_id),
        status=value["status"],
        written=written,
    )
    return _done(job.product_id, value["status"] if written else "skipped")


async def _request(ctx: JobContext, job: _Job) -> ClassifyRequest:
    images = await load_context_images(get_storage(), ctx.tenant_id, job.inputs.images)
    return ClassifyRequest(
        tenant_id=ctx.tenant_id,
        product_id=job.product_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        text=ProductText.model_validate(job.inputs.text),
        images=images,
        categories=[
            CategoryOption(
                category=offer.category,
                key_attributes=list(offer.key_attributes),
                template_version=offer.template_version,
            )
            for offer in job.inputs.offers
        ],
    )


def _checked(ctx: JobContext, job: _Job, result: object) -> ClassifyResult | str:
    """The answer re-validated through JSON with its records trusted and restamped, or the name
    of the check it failed."""
    if not isinstance(result, ClassifyResult):
        return "type"
    try:
        checked = ClassifyResult.model_validate_json(result.model_dump_json())
    except (ValidationError, ValueError, TypeError):
        return "schema"
    records = trust_records(
        checked.agent_runs,
        prompt_prefix=PROMPT_PREFIX,
        tenant_id=ctx.tenant_id,
        product_id=job.product_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        attempt=min(max(ctx.attempt, 1), 3),
        claim_started_at=job.claim_started_at,
        now=datetime.now(UTC),
        agent_name=AGENT,
        triggers=TRIGGERS,
    )
    if isinstance(records, str):
        return records
    return checked.model_copy(update={"agent_runs": list(records)})


def clean_rationale(text: str) -> str:
    """One line of printable text: control and format characters (NUL, bidi overrides) removed,
    whitespace collapsed, at most 300 characters."""
    kept = "".join(
        ch if not unicodedata.category(ch).startswith("C") else " "
        for ch in unicodedata.normalize("NFC", text)
    )
    return " ".join(kept.split())[:MAX_RATIONALE]


def _refs(refs: list[str], request: ClassifyRequest) -> list[str]:
    images = {str(image.image_id) for image in request.images}
    out: list[str] = []
    for ref in refs:
        if len(out) >= MAX_REFS or len(ref) > MAX_REF_CHARS or ref in out:
            continue
        if (
            (ref.startswith("text.") and ref[5:] in TEXT_REFS and getattr(request.text, ref[5:]))
            or (ref.startswith("attributes.") and ref[11:] in request.text.attributes)
            or ((found := _IMAGE_REF.fullmatch(ref)) is not None and found.group(1) in images)
        ):
            out.append(ref)
    return out


def screen(result: ClassifyResult, request: ClassifyRequest) -> ClassifyResult:
    """Only offered categories, once each, highest confidence first, at most 3, with a clean
    rationale and only the evidence refs that point at a sent input."""
    offered = {option.category for option in request.categories}
    seen: set[str] = set()
    kept = []
    for candidate in sorted(result.candidates, key=lambda c: -c.confidence):
        if candidate.category not in offered or candidate.category in seen:
            continue
        seen.add(candidate.category)
        kept.append(
            candidate.model_copy(
                update={
                    "rationale": clean_rationale(candidate.rationale),
                    "evidence_refs": _refs(candidate.evidence_refs, request),
                }
            )
        )
    return result.model_copy(update={"candidates": kept[:MAX_CANDIDATES]})


def _status(job: _Job, product_category: str | None, result: ClassifyResult) -> str:
    """`suggested`, or settled when the seller wrote another category after the ask."""
    if not job.asked_known or product_category is None or product_category == job.asked_category:
        return "suggested"
    candidates = {c.category for c in result.candidates}
    return "confirmed" if product_category in candidates else "overridden"


def _needs_review(result: ClassifyResult) -> bool:
    if not result.candidates:
        return True
    return result.candidates[0].confidence < CLASSIFY_CONFIDENCE_TASK_THRESHOLD


async def _store_suggested(ctx: JobContext, job: _Job, result: ClassifyResult) -> Outcome:
    """The fence, the suggestion and (when unsure) the review task, in one unit."""
    from app.core.config import get_settings

    async with tenant_uow(ctx.tenant_id) as uow:
        product = await uow.products.get_for_update(job.product_id)
        if product is None or product.parent_product_id is not None:
            return _done(job.product_id, "skipped")
        now_inputs = await suggest_inputs(uow, product, get_settings())
        if now_inputs.digest != job.inputs.digest:  # edited during the call: stale
            written = await uow.products.write_category_suggestion(
                job.product_id, _value("failed", job, ctx), keep_settled=True
            )
            log.info(
                "category_suggestion.stale",
                product_id=str(job.product_id),
                job_run_id=str(ctx.job_run_id),
                written=written,
            )
            return _done(job.product_id, "failed" if written else "skipped")
        stored = product.category_suggestion
        if (
            isinstance(stored, dict)
            and stored.get("status") in SETTLED_SUGGESTION_STATUSES
            and stored.get("input_digest") == job.inputs.digest
        ):  # the seller already decided on these inputs
            log.info(
                "category_suggestion.kept_settled",
                product_id=str(job.product_id),
                job_run_id=str(ctx.job_run_id),
                status=stored.get("status"),
            )
            return _done(job.product_id, "skipped")
        status = _status(job, product.category, result)
        await uow.products.write_category_suggestion(
            job.product_id, _value(status, job, ctx, result)
        )
        task = False
        if status == "suggested" and product.category is None and _needs_review(result):
            task = await uow.human_tasks.open_category_review(
                job.product_id,
                reason=REVIEW_REASON,
                allowed=[offer.category for offer in job.inputs.offers],
                suggestions=[c.category for c in result.candidates],
            )
    log.info(
        "category_suggestion.stored",
        product_id=str(job.product_id),
        job_run_id=str(ctx.job_run_id),
        status=status,
        candidates=len(result.candidates),
        task_opened=task,
    )
    return _done(job.product_id, status)


async def run_category_suggestion(ctx: JobContext) -> Outcome:
    from app.core.config import get_settings
    from app.services.pipeline_deps import SeamPortError, classify_credentials
    from app.workers import classify_seam
    from app.workers.budget import CLASSIFY_RESERVATION_USD, BudgetRefused, reserve

    product_id = _product_id(ctx)
    job = await _read(ctx, product_id)
    if job is None:
        log.info(
            "category_suggestion.skipped",
            product_id=str(product_id),
            job_run_id=str(ctx.job_run_id),
        )
        return _done(product_id, "skipped")
    settings = get_settings()
    if not settings.category_suggest_enabled or not job.inputs.offers:
        return await _settle(ctx, job, _value("unavailable", job, ctx))
    try:
        credentials = classify_credentials(settings)
    except SeamPortError:
        return await _settle(ctx, job, _value("unavailable", job, ctx))
    try:
        reserved = await reserve(
            ctx.tenant_id, ctx.job_run_id, ctx.attempt, amount=CLASSIFY_RESERVATION_USD
        )
    except BudgetRefused:
        return await _settle(ctx, job, _value("failed", job, ctx))
    if not reserved:  # the fence moved: a newer attempt owns the row
        raise _failed("the classification job lost its claim", ctx)
    request = await _request(ctx, job)
    try:
        result = await asyncio.wait_for(
            classify_seam.call_classify_product(request, credentials),
            timeout=credentials.timeout_seconds + SEAM_GRACE_S,
        )
    except Exception as error:  # a timeout or a pipeline bug: the job policy retries
        log.info(
            "category_suggestion.seam_failed",
            product_id=str(product_id),
            job_run_id=str(ctx.job_run_id),
            error=type(error).__name__,
        )
        raise RetryableJobError(ErrorCode.JOB_TIMEOUT) from None
    if result is None:  # the installed pipeline has no classify_product: no call was made
        return await _settle(ctx, job, _value("unavailable", job, ctx))
    checked = _checked(ctx, job, result)
    if isinstance(checked, str):
        log.info(
            "category_suggestion.untrusted",
            product_id=str(product_id),
            job_run_id=str(ctx.job_run_id),
            check=checked,
        )
        raise _failed("the classification answer was not trusted", ctx)
    async with tenant_uow(ctx.tenant_id) as uow:  # invariant 7: the records first
        for record in checked.agent_runs:
            await uow.agent_runs.insert(record)
    if checked.status == "failed" or checked.error is not None:
        return await _settle(ctx, job, _value("failed", job, ctx))
    return await _store_suggested(ctx, job, screen(checked, request))
