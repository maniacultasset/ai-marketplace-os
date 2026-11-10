"""The image candidate job body (SI7 T11-T12, ADR-0030 proposed): `run_image_candidate(ctx)`.

A candidate job is an `image_compliance` job whose payload names a `candidate_id`
(`app.workers.image_jobs` dispatches). Steps for the zero-token kinds (`fix`, `size_chart`):

1. One short read: the candidate (gone, owned by another job or not `queued` → `succeeded`
   with `skipped`); for `fix` the source row (still the live `main`, same `clean_content_hash`,
   with its clean copy, else `failed/source_changed`); for `size_chart` the product's chart (gone
   or changed → `source_changed`). `source_changed` is a state, not a verdict: it stays outside
   the cache index, so asking again makes a new candidate.
2. GET the clean copy (10 MiB cap; a store outage is retried).
3. The child op in the killable child. A verdict is stored (`too_small`, `source_unusable`,
   `not_white`); an infrastructure failure (`CandidateChildFailed`) is retried, and after the
   last attempt the terminal hook ends the row `failed/model_failed` (CP-D19: never cached).
4. PUT the object under `candidate_key` (an outage is retried).
5. `mark_ready` (fenced on this job and `queued`), then the outcome.

No model call and no `agent_runs` row for these kinds. Log lines carry ids, the op and the
reason only, never pixels, keys or signed URLs.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID, uuid5

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind, Marketplace

from app.adapters.storage import (
    StorageNotFound,
    StorageObjectTooLarge,
    StorageUnavailable,
    get_storage,
    images_bucket,
)
from app.core.logging import get_logger
from app.db.models.catalog_assets import Image, ImageCandidate
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services import image_runner
from app.services.category_gate import rules_category
from app.services.image_candidates import size_chart_source
from app.services.image_edit_result import EditTrusted, EditUntrusted, check_edit_result
from app.services.image_keys import CLEAN_NAMES, candidate_key, derived_key
from app.services.image_runner import CandidateChildFailed, CandidateReply
from app.services.rules_loader import RuleLoadError, load_rules
from app.workers.errors import JobFailed, RetryableJobError, system_report
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
MAX_IMAGE_BYTES: Final = 10_485_760
# the hard deadline over the seam call: the credentials' timeout plus this grace
SEAM_GRACE_S: Final = 15
# the source changed or went away before the job ran (never cached, unlike a child verdict)
SOURCE_CHANGED: Final = "source_changed"
# a fidelity refusal gets one fresh edit of the same input before the seller sees it
FIDELITY_TRIES: Final = 2
# the child work after the retry's seam call (finish, store) must still fit in the body timeout
RETRY_MARGIN_S: Final = 30


def candidate_id_of(ctx: JobContext) -> UUID:
    try:
        return UUID(str(ctx.payload["candidate_id"]))
    except (KeyError, ValueError):
        raise _failed("the candidate job has no candidate id", ctx) from None


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


def _done(candidate_id: UUID, status: str, **extra: Any) -> Outcome:
    return Outcome("succeeded", {"candidate_id": str(candidate_id), "status": status, **extra})


async def main_rules(uow: TenantUnitOfWork, ctx: JobContext, category: str) -> dict[str, Any]:
    """The resolved `images` rules per marketplace, as JSON for the child (as the compliance job
    loads them)."""
    out: dict[str, Any] = {}
    for market in Marketplace:
        try:
            loaded = await load_rules(
                uow, tenant_id=ctx.tenant_id, marketplace=market, category=category
            )
        except RuleLoadError:
            raise _failed("the image rules cannot be loaded", ctx) from None
        out[market.value] = [
            r.model_dump(mode="json", by_alias=True)
            for r in loaded.ruleset.rules
            if r.field == "images"
        ]
    return out


async def fail(ctx: JobContext, candidate_id: UUID, reason: str, **extra: Any) -> Outcome:
    async with tenant_uow(ctx.tenant_id) as uow:
        await uow.image_candidates.mark_failed(
            candidate_id, ctx.job_run_id, reason, fidelity=extra.get("fidelity")
        )
    log.info(
        "image_candidate.failed",
        candidate_id=str(candidate_id),
        job_run_id=str(ctx.job_run_id),
        reason=reason,
    )
    return _done(candidate_id, "failed", reason=reason)


async def read_object(key: str) -> bytes:
    try:
        return await get_storage().get(images_bucket(), key, max_bytes=MAX_IMAGE_BYTES)
    except StorageUnavailable:
        raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None


def clean_key_of(tenant_id: UUID, source: Image) -> str | None:
    if source.clean_mime_type not in CLEAN_NAMES:
        return None
    return derived_key(tenant_id, source.product_id, source.id, CLEAN_NAMES[source.clean_mime_type])


def source_is_current(candidate: ImageCandidate, source: Image | None) -> bool:
    """The source is still the product's live main with the hash the candidate was made for."""
    return (
        source is not None
        and source.deleted_at is None
        and source.slot == "main"
        and source.product_id == candidate.product_id
        and source.clean_content_hash == candidate.source_hash
    )


async def store_ready(
    ctx: JobContext,
    candidate: ImageCandidate,
    reply: CandidateReply,
    *,
    fidelity: dict[str, Any] | None = None,
) -> Outcome:
    assert reply.image is not None and reply.width and reply.height  # noqa: S101 - an ok reply
    key = candidate_key(ctx.tenant_id, candidate.product_id, candidate.id)
    try:
        await get_storage().put(images_bucket(), key, reply.image, "image/jpeg")
    except StorageUnavailable:
        raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None
    async with tenant_uow(ctx.tenant_id) as uow:
        written = await uow.image_candidates.mark_ready(
            candidate.id,
            ctx.job_run_id,
            storage_key=key,
            width=reply.width,
            height=reply.height,
            fidelity=fidelity,
        )
    log.info(
        "image_candidate.ready",
        candidate_id=str(candidate.id),
        job_run_id=str(ctx.job_run_id),
        kind=candidate.kind,
        written=written,
    )
    return _done(candidate.id, "ready" if written else "skipped")


async def _deterministic(ctx: JobContext, candidate: ImageCandidate) -> Outcome:
    """`fix` and `size_chart`: zero tokens, retried on infrastructure failures."""
    inputs: dict[str, bytes] = {}
    params: dict[str, Any]
    async with tenant_uow(ctx.tenant_id) as uow:
        product = await uow.products.get_by_id(candidate.product_id)
        if product is None:
            return await fail(ctx, candidate.id, SOURCE_CHANGED)
        if candidate.kind == "size_chart":
            source = size_chart_source(product.attributes)
            if source is None:
                return await fail(ctx, candidate.id, SOURCE_CHANGED)
            if source.source_hash != candidate.source_hash:
                return await fail(ctx, candidate.id, SOURCE_CHANGED)
            params = {"chart": dict(source.chart), "fibres": source.fibres}
            op = "size_chart"
            clean_key = None
        else:
            row = (
                None
                if candidate.source_image_id is None
                else await uow.images.get_live(candidate.source_image_id)
            )
            if not source_is_current(candidate, row):
                return await fail(ctx, candidate.id, SOURCE_CHANGED)
            assert row is not None  # noqa: S101 - source_is_current
            clean_key = clean_key_of(ctx.tenant_id, row)
            if clean_key is None:
                return await fail(ctx, candidate.id, SOURCE_CHANGED)
            params = {
                "rules": await main_rules(uow, ctx, rules_category(product.category)),
                "image_id": str(candidate.id),
                "now": datetime.now(UTC).isoformat(),
            }
            op = "fix"
    if clean_key is not None:
        try:
            inputs["clean"] = await read_object(clean_key)
        except (StorageNotFound, StorageObjectTooLarge):
            return await fail(ctx, candidate.id, SOURCE_CHANGED)
    try:
        reply = await image_runner.run_candidate_isolated(op, inputs, params)
    except CandidateChildFailed as error:
        log.info(
            "image_candidate.child_failed",
            candidate_id=str(candidate.id),
            job_run_id=str(ctx.job_run_id),
            op=op,
            reason=error.reason,
        )
        raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None
    if not reply.ok:
        return await fail(ctx, candidate.id, reply.reason or "source_unusable")
    return await store_ready(ctx, candidate, reply)


async def run_image_candidate(ctx: JobContext) -> Outcome:
    candidate_id = candidate_id_of(ctx)
    async with tenant_uow(ctx.tenant_id) as uow:
        candidate = await uow.image_candidates.get(candidate_id)
    if candidate is None:
        return _done(candidate_id, "skipped", skipped="candidate_gone")
    if candidate.job_id != ctx.job_run_id or candidate.status != "queued":
        return _done(candidate_id, "skipped", skipped=candidate.status)
    if candidate.kind == "white_background":
        return await _white_background(ctx, candidate)
    return await _deterministic(ctx, candidate)


# ---- T12: the AI white-background path ----


def _zeroed(outcome: Outcome) -> Outcome:
    return Outcome(outcome.status, outcome.result, zero_reservation=True)


async def _ai_fail(
    ctx: JobContext, candidate_id: UUID, reason: str, *, zero: bool, **extra: Any
) -> Outcome:
    outcome = await fail(ctx, candidate_id, reason, **extra)
    return _zeroed(outcome) if zero else outcome


@dataclass(frozen=True, slots=True)
class _EditInputs:
    category: str
    clean_key: str
    vision_key: str
    rules: dict[str, Any]
    claim_started_at: datetime


async def _edit_inputs(ctx: JobContext, candidate: ImageCandidate) -> _EditInputs | None:
    async with tenant_uow(ctx.tenant_id) as uow:
        product = await uow.products.get_by_id(candidate.product_id)
        row = (
            None
            if candidate.source_image_id is None
            else await uow.images.get_live(candidate.source_image_id)
        )
        if product is None or not source_is_current(candidate, row):
            return None
        assert row is not None  # noqa: S101 - source_is_current
        clean = clean_key_of(ctx.tenant_id, row)
        if clean is None:
            return None
        job = await uow.job_runs.get_by_id(ctx.job_run_id)
        return _EditInputs(
            category=rules_category(product.category),
            clean_key=clean,
            vision_key=derived_key(ctx.tenant_id, row.product_id, row.id, "vision.jpg"),
            rules=await main_rules(uow, ctx, rules_category(product.category)),
            claim_started_at=(job.started_at if job else None) or datetime.now(UTC),
        )


def edit_attempt(job_attempt: int, try_no: int) -> int:
    """The `attempt` the edit request carries: the job's attempt on the first try, one more for
    the fidelity retry (at most 3, the schema's bound). The pipeline (>= 0.7.1) samples a later
    attempt anew (a new seed, above temperature 0); a byte-identical retry mostly repeated the
    refused image at full price. The stored records still carry the job's attempt
    (`check_edit_result` restamps them)."""
    return min(max(job_attempt, 1) + try_no - 1, 3)


async def _call_model(
    ctx: JobContext,
    candidate: ImageCandidate,
    inputs: _EditInputs,
    prepared: CandidateReply,
    try_no: int = 1,
) -> Any:
    """The seam call under a hard deadline. Returns the result, None (no `edit_image` in the
    installed pipeline) or raises."""
    from mkos_schemas.image_edit import ImageEditRequest
    from mkos_schemas.jobs import ContextImage

    from app.core.config import get_settings
    from app.services.pipeline_deps import image_edit_credentials
    from app.workers import image_edit_seam

    assert prepared.image is not None and prepared.width and prepared.height  # noqa: S101
    credentials = image_edit_credentials(get_settings())
    request = ImageEditRequest(
        tenant_id=ctx.tenant_id,
        product_id=candidate.product_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        attempt=edit_attempt(ctx.attempt, try_no),
        task="white_background",
        image=ContextImage(
            image_id=candidate.source_image_id,
            slot="main",
            width=prepared.width,
            height=prepared.height,
            data_b64=base64.b64encode(prepared.image).decode("ascii"),
        ),
        category=inputs.category[:64],
    )
    return await asyncio.wait_for(
        image_edit_seam.call_edit_image(request, credentials),
        timeout=credentials.timeout_seconds + SEAM_GRACE_S,
    )


async def _store_runs(
    ctx: JobContext,
    candidate: ImageCandidate,
    trusted: EditTrusted,
    *,
    taken: set[UUID],
    prior_cost: Decimal,
    try_no: int,
) -> Decimal:
    """Invariant 7: every model call's record, before any pixel work, in one short unit. Each try
    stores its own records: an id an earlier try of this job already stored (a deterministic
    pipeline id) is re-derived from it, so the retry's record is never dropped as a duplicate.
    The candidate points at this try's record and carries the cost of every try. Returns the
    cost of every try so far."""
    records = [
        (
            each
            if each.id not in taken
            else each.model_copy(update={"id": uuid5(each.id, f"fidelity_retry:{try_no}")})
        )
        for each in trusted.records
    ]
    record = records[0] if records else None
    cost = prior_cost + (record.cost_usd if record is not None else Decimal(0))
    async with tenant_uow(ctx.tenant_id) as uow:
        for each in records:
            await uow.agent_runs.insert(each)
        if record is not None:
            await uow.image_candidates.record_run(
                candidate.id,
                ctx.job_run_id,
                agent_run_id=record.id,
                cost_usd=cost,
                served_provider=trusted.result.served_provider,
            )
    taken.update(each.id for each in records)
    log.info(
        "image_candidate.model_done",
        candidate_id=str(candidate.id),
        job_run_id=str(ctx.job_run_id),
        status=trusted.result.status,
        served_provider=trusted.result.served_provider,
        cost_usd=str(record.cost_usd) if record is not None else "0",
        try_no=try_no,
    )
    return cost


def _refused_reason(refused: Any) -> str:
    """A claim refusal's candidate reason: the AI pause for its credit reason, else the cap."""
    if refused.scope == "credit":
        return "credit_used_up" if refused.reason == "credit_exhausted" else "model_failed"
    return "budget"


async def _provider_refusal(ctx: JobContext, code: ErrorCode | None, checked: EditTrusted) -> str:
    """A failed edit's candidate reason, and the platform pause on a provider account refusal
    (SI10 D5): a credit refusal (the 402, as `AI_CREDIT_EXHAUSTED` or the legacy
    `BUDGET_EXCEEDED`) pauses AI for everyone; a key refusal pauses only once OpenRouter's
    `/key` confirms it (a 401/403 may be a moderation refusal of tenant input)."""
    from app.services.ai_credit import EmbedRefusal, classify_refusal
    from app.workers import budget

    said: EmbedRefusal | None = None
    if code in (ErrorCode.AI_CREDIT_EXHAUSTED, ErrorCode.BUDGET_EXCEEDED):
        said = "credit"
    elif code is ErrorCode.AI_PROVIDER_KEY_INVALID:
        said = "key_suspect"
    refusal = classify_refusal(checked.records, said)
    if refusal == "credit":
        await budget.pause_ai("credit_exhausted", job_id=ctx.job_run_id)
        return "credit_used_up"
    if refusal == "key_suspect" and await budget.confirm_key_refusal():
        await budget.pause_ai("key_invalid", job_id=ctx.job_run_id)
    return "model_failed"


@dataclass(frozen=True, slots=True)
class _Unfaithful:
    """A try whose output the fidelity judge refused (its record is stored)."""

    fidelity: dict[str, Any] | None
    cost: Decimal


def _time_for_a_retry(inputs: _EditInputs, seam_s: float) -> bool:
    """The retry's seam call fits in what is left of the job's body timeout, with a margin for
    the child work after it."""
    used = (datetime.now(UTC) - inputs.claim_started_at).total_seconds()
    left = POLICIES[JobKind.IMAGE_COMPLIANCE].timeout_s - used
    return left > seam_s + SEAM_GRACE_S + RETRY_MARGIN_S


def _seam_timeout_s(settings: Any) -> float:
    from app.services.pipeline_deps import image_edit_credentials

    return float(image_edit_credentials(settings).timeout_seconds)


async def _white_background(ctx: JobContext, candidate: ImageCandidate) -> Outcome:
    """A paid edit, the job never retried (`max_attempts=1`). A fidelity refusal gets ONE fresh
    edit of the same prepared input (FIDELITY_TRIES), sent as the next `attempt` so the pipeline
    samples it anew (`edit_attempt`), only when the D21 limits and the budget
    caps still admit it: the reservation is re-taken through `reserve` (credit, the platform's
    image share and the tenant and global caps are checked again). The retry never counts again
    toward the tenant's daily candidate limit: the limit counts candidates and the seller asked
    once. Each try stores its own `agent_runs` record. A second refusal, or a retry the caps
    refuse, ends as the first refusal would have (`fidelity`).

    The reservation is zeroed whenever no model call was made or its record is stored; an
    untrusted result, a seam exception or a timeout keeps it counted (the call may have been
    billed, its record is lost)."""
    from app.core.config import get_settings
    from app.workers.budget import (
        IMAGE_EDIT_RESERVATION_USD,
        BudgetRefused,
        ImageEditLimits,
        release,
        reserve,
    )

    settings = get_settings()
    if not settings.image_edit_enabled:
        return await _ai_fail(ctx, candidate.id, "model_failed", zero=True)
    inputs = await _edit_inputs(ctx, candidate)
    if inputs is None:
        return await _ai_fail(ctx, candidate.id, SOURCE_CHANGED, zero=True)
    limits = ImageEditLimits.from_settings(settings, exclude_id=candidate.id)

    async def take() -> None:
        reserved = await reserve(
            ctx.tenant_id,
            ctx.job_run_id,
            ctx.attempt,
            amount=IMAGE_EDIT_RESERVATION_USD,
            image_edit=limits,
        )
        if not reserved:  # the fence moved: a newer attempt owns the row
            raise _failed("the candidate job lost its claim", ctx)

    try:
        await take()
    except BudgetRefused as refused:  # SI10 D4: AI paused is not the daily budget
        return await _ai_fail(ctx, candidate.id, _refused_reason(refused), zero=True)
    try:
        vision = await read_object(inputs.vision_key)
        clean = await read_object(inputs.clean_key)
    except (StorageNotFound, StorageObjectTooLarge):
        return await _ai_fail(ctx, candidate.id, SOURCE_CHANGED, zero=True)
    except RetryableJobError:
        return await _ai_fail(ctx, candidate.id, "model_failed", zero=True)
    try:
        prepared = await image_runner.run_candidate_isolated("prepare", {"vision": vision}, {})
    except CandidateChildFailed:
        return await _ai_fail(ctx, candidate.id, "model_failed", zero=True)
    if not prepared.ok:
        return await _ai_fail(ctx, candidate.id, prepared.reason or "source_unusable", zero=True)

    taken: set[UUID] = set()
    cost = Decimal(0)
    try_no = 1
    while True:
        step = await _edit_try(ctx, candidate, inputs, prepared, clean, taken, cost, try_no)
        if not isinstance(step, _Unfaithful):
            return step
        cost = step.cost
        retry = try_no < FIDELITY_TRIES and _time_for_a_retry(inputs, _seam_timeout_s(settings))
        if retry:
            # this try's spend is in its stored record: settle it before the caps look again
            await release(ctx.tenant_id, ctx.job_run_id, ctx.attempt)
            try:
                await take()
            except BudgetRefused as refused:
                log.info(
                    "image_candidate.fidelity_retry_refused",
                    candidate_id=str(candidate.id),
                    job_run_id=str(ctx.job_run_id),
                    scope=refused.scope,
                )
                retry = False
        if not retry:
            return await _ai_fail(ctx, candidate.id, "fidelity", zero=True, fidelity=step.fidelity)
        try_no += 1
        log.info(
            "image_candidate.fidelity_retry",
            candidate_id=str(candidate.id),
            job_run_id=str(ctx.job_run_id),
            try_no=try_no,
        )


async def _edit_try(
    ctx: JobContext,
    candidate: ImageCandidate,
    inputs: _EditInputs,
    prepared: CandidateReply,
    clean: bytes,
    taken: set[UUID],
    prior_cost: Decimal,
    try_no: int,
) -> Outcome | _Unfaithful:
    """One model call and its finish: a terminal outcome, or `_Unfaithful` for a fidelity
    refusal (the caller decides whether to try once more)."""
    from app.services.pipeline_deps import SeamPortError

    try:
        result = await _call_model(ctx, candidate, inputs, prepared, try_no)
    except SeamPortError:
        return await _ai_fail(ctx, candidate.id, "model_failed", zero=True)
    except Exception as error:
        log.info(
            "image_candidate.seam_failed",
            candidate_id=str(candidate.id),
            job_run_id=str(ctx.job_run_id),
            error=type(error).__name__,
            try_no=try_no,
        )
        return await _ai_fail(ctx, candidate.id, "model_failed", zero=False)
    if result is None:  # the installed pipeline has no edit_image: no call was made
        return await _ai_fail(ctx, candidate.id, "model_failed", zero=True)
    checked = check_edit_result(
        result,
        tenant_id=ctx.tenant_id,
        product_id=candidate.product_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        attempt=min(max(ctx.attempt, 1), 3),
        claim_started_at=inputs.claim_started_at,
        now=datetime.now(UTC),
    )
    if isinstance(checked, EditUntrusted):
        log.info(
            "image_candidate.untrusted",
            candidate_id=str(candidate.id),
            job_run_id=str(ctx.job_run_id),
            check=checked.check,
            try_no=try_no,
        )
        return await _ai_fail(ctx, candidate.id, "model_failed", zero=False)
    cost = await _store_runs(
        ctx, candidate, checked, taken=taken, prior_cost=prior_cost, try_no=try_no
    )
    edit = checked.result
    if edit.status == "failed" or edit.image_b64 is None:
        code = edit.error.error_code if edit.error is not None else None
        reason = await _provider_refusal(ctx, code, checked)
        return await _ai_fail(ctx, candidate.id, reason, zero=True)
    output = base64.b64decode(edit.image_b64)
    params = {
        "rules": inputs.rules,
        "image_id": str(candidate.id),
        "now": datetime.now(UTC).isoformat(),
    }
    try:
        reply = await image_runner.run_candidate_isolated(
            "finish_white", {"output": output, "clean": clean}, params
        )
    except CandidateChildFailed:  # CP-D19: never cached; the record is already stored
        return await _ai_fail(ctx, candidate.id, "model_failed", zero=True)
    if not reply.ok and reply.reason == "fidelity":
        log.info(
            "image_candidate.unfaithful",
            candidate_id=str(candidate.id),
            job_run_id=str(ctx.job_run_id),
            try_no=try_no,
        )
        return _Unfaithful(fidelity=reply.fidelity, cost=cost)
    if not reply.ok:
        return await _ai_fail(
            ctx,
            candidate.id,
            reply.reason or "source_unusable",
            zero=True,
            fidelity=reply.fidelity,
        )
    try:
        stored = await store_ready(ctx, candidate, reply, fidelity=reply.fidelity)
    except RetryableJobError:  # no retry for a paid job: the seller asks again
        return await _ai_fail(ctx, candidate.id, "model_failed", zero=True)
    return _zeroed(stored)
