"""The draft classification job body (client feedback 11 Nov, item 5): `run_category_draft(ctx)`.

A draft classification is an `image_compliance` job (`entity_type='category_suggestion'`, an
entity id derived from the draft's digest: a draft has no product row) whose payload names
`category_draft` with the draft text and its digest (`app.services.category_draft`;
`app.workers.image_jobs` dispatches). Its answer is the
job's `result_json`: `{status, candidates, calibration_version}`. It writes no product, fact,
provenance, task or listing: the seller picks a candidate on the add form.

1. The payload's text is re-validated as `ProductText` (an invalid one fails the job).
2. Switched off, nothing to offer, no classify credentials, or no `classify_product` in the
   installed pipeline: `unavailable`, with no call and nothing spent.
3. Reserve `classify_reservation_usd` (a refusal answers `failed`), then one seam call under a
   hard deadline, with no photos. The wire `product_id` is the job id (the pipeline only echoes
   it); the stored records carry no product.
4. The answer is re-validated through JSON and every record is checked and restamped from the job
   (`trust_records`, as the product body). An untrusted answer fails the job. The records are
   stored in their own unit first (invariant 7).
5. Screening as the product body (`screen`): offered categories only, once each, highest
   confidence first, at most 3, one clean line of rationale.

A timeout or a seam exception is retried by the job policy. Log lines carry ids, counts and
statuses only, never the draft text.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mkos_schemas.classify import CategoryOption, ClassifyRequest, ClassifyResult, ProductText
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from pydantic import BaseModel, ValidationError

from app.core.logging import get_logger
from app.repositories.uow import tenant_uow
from app.services.category_gate import CategoryOffer, category_offers
from app.services.image_edit_result import trust_records
from app.workers.category_suggestion import AGENT, PROMPT_PREFIX, SEAM_GRACE_S, TRIGGERS, screen
from app.workers.errors import JobFailed, RetryableJobError, system_report
from app.workers.policy import CATEGORY_DRAFT_PAYLOAD as DRAFT_KEY
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

__all__ = ["DRAFT_KEY", "run_category_draft"]

log = get_logger(__name__)


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


def _text(ctx: JobContext) -> ProductText:
    payload = ctx.payload.get(DRAFT_KEY)
    try:
        return ProductText.model_validate(payload["text"])  # type: ignore[index]
    except (KeyError, TypeError, ValidationError):
        raise _failed("the draft classification job has no valid text", ctx) from None


def _answer(status: str, result: ClassifyResult | None = None) -> Outcome:
    body: dict[str, Any] = {
        "status": status,
        "candidates": (
            []
            if result is None
            else [
                {"category": c.category, "confidence": c.confidence, "rationale": c.rationale}
                for c in result.candidates
            ]
        ),
        "calibration_version": None if result is None else result.calibration_version[:32],
    }
    return Outcome("succeeded", body, zero_reservation=True)


def _request(ctx: JobContext, text: ProductText, offers: list[CategoryOffer]) -> ClassifyRequest:
    return ClassifyRequest(
        tenant_id=ctx.tenant_id,
        product_id=ctx.job_run_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        text=text,
        images=[],
        categories=[
            CategoryOption(
                category=offer.category,
                key_attributes=list(offer.key_attributes),
                template_version=offer.template_version,
            )
            for offer in offers
        ],
    )


def _checked(ctx: JobContext, result: object, claim_started_at: datetime) -> ClassifyResult | str:
    """The answer re-validated through JSON with its records trusted and restamped (no
    product), or the name of the check it failed."""
    if not isinstance(result, BaseModel):
        return "type"
    try:
        checked = ClassifyResult.model_validate_json(result.model_dump_json())
    except (ValidationError, ValueError, TypeError):
        return "schema"
    records = trust_records(
        checked.agent_runs,
        prompt_prefix=PROMPT_PREFIX,
        tenant_id=ctx.tenant_id,
        product_id=None,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        attempt=min(max(ctx.attempt, 1), 3),
        claim_started_at=claim_started_at,
        now=datetime.now(UTC),
        agent_name=AGENT,
        triggers=TRIGGERS,
    )
    if isinstance(records, str):
        return records
    return checked.model_copy(update={"agent_runs": list(records)})


async def _read(ctx: JobContext) -> tuple[list[CategoryOffer], datetime]:
    from app.core.config import get_settings

    async with tenant_uow(ctx.tenant_id) as uow:
        offers = await category_offers(uow, get_settings())
        job = await uow.job_runs.get_by_id(ctx.job_run_id)
    return offers, (job.started_at if job else None) or datetime.now(UTC)


async def run_category_draft(ctx: JobContext) -> Outcome:
    from app.core.config import get_settings
    from app.services.pipeline_deps import SeamPortError, classify_credentials
    from app.workers import classify_seam
    from app.workers.budget import BudgetRefused, classify_reservation_usd, reserve

    job_id: UUID = ctx.job_run_id
    text = _text(ctx)
    settings = get_settings()
    offers, claim_started_at = await _read(ctx)
    if not settings.category_suggest_enabled or not offers:
        return _answer("unavailable")
    try:
        credentials = classify_credentials(settings)
    except SeamPortError:
        return _answer("unavailable")
    try:
        reserved = await reserve(
            ctx.tenant_id, job_id, ctx.attempt, amount=classify_reservation_usd(settings)
        )
    except BudgetRefused:
        return _answer("failed")
    if not reserved:  # the fence moved: a newer attempt owns the row
        raise _failed("the draft classification job lost its claim", ctx)
    request = _request(ctx, text, offers)
    try:
        result = await asyncio.wait_for(
            classify_seam.call_classify_product(request, credentials),
            timeout=credentials.timeout_seconds + SEAM_GRACE_S,
        )
    except Exception as error:  # a timeout or a pipeline bug: the job policy retries
        log.info("category_draft.seam_failed", job_run_id=str(job_id), error=type(error).__name__)
        raise RetryableJobError(ErrorCode.JOB_TIMEOUT) from None
    if result is None:  # the installed pipeline has no classify_product: no call was made
        return _answer("unavailable")
    checked = _checked(ctx, result, claim_started_at)
    if isinstance(checked, str):
        log.info("category_draft.untrusted", job_run_id=str(job_id), check=checked)
        raise _failed("the draft classification answer was not trusted", ctx)
    async with tenant_uow(ctx.tenant_id) as uow:  # invariant 7: the records first
        for record in checked.agent_runs:
            await uow.agent_runs.insert(record)
    if checked.status == "failed" or checked.error is not None:
        return _answer("failed")
    screened = screen(checked, request)
    log.info("category_draft.stored", job_run_id=str(job_id), candidates=len(screened.candidates))
    return _answer("suggested", screened)
