"""The `listing_generate` job body (S18 T16; DW1-3; BACKEND-ARCHITECTURE.md:704-711).

The only module that imports `mkos_pipeline` (the S14b stub locally and in CI; the AI team's tagged
drop at deploy, T104). No session is open across the pipeline call. The steps:

1. `budget.reserve` (T7): the claim-time reservation against the tenant and global caps. A refusal
   → `JobFailed(BUDGET_EXCEEDED)`; the terminal hook moves the listing to
   `failed/generation_failed`.
2. One short read transaction: the job, the listing, `load_rules`, the resolved facts, then
   `build_context` (T11). A failure here made no model call: the reservation is zeroed.
3. `build_pipeline_deps(..., product=for_rules(facts))` (CP13), then `run_listing_pipeline(ctx,
   deps)` with no transaction open; `deps.close_spans()` whatever happens.
4. `check_result` → `recheck` → `map_outcome` → `make_persist` → an `Outcome` (a version saved;
   the reservation zeroed, real spend replaces it) or a `JobFailed` (no version, the returned
   `agent_runs` saved and replacing the reservation). When every agent of a no-version result that
   ended on a failed run ended on a transient one (rate limit, provider down, timeout), and no
   failed block's agent ended on a completed run, the report is a retryable `MODEL_UNAVAILABLE`,
   not the seller-facing `GENERATION_FAILED` (`runs_failed_transiently`).
5. Any port or pipeline exception → `JobFailed` with a fixed message (the class name is logged
   only) and the recorded rules runs kept: `MODEL_UNAVAILABLE` (retryable) for a transient
   model or embedding port failure (`is_transient_failure`: a transient code and `retryable`),
   else `INTERNAL_ERROR` (a missing key or embedder, or a refused embeddings call, included). The
   reservation is zeroed when the run never reached a model stage
   (`deps.model_stage_reached`: only the context load and the brand retrieval started, whose
   query embeds write their own `agent_runs`), and kept otherwise, because the pipeline's
   records of calls already made are lost with the exception (CP10).

A transient failure is not retried inside the job: `listing_generate` keeps `max_attempts=1`
(`policy.py`); the seller regenerates.

Log lines carry ids and codes only: no instruction, product or model text.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Final

from mkos_pipeline import run_listing_pipeline
from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind, Marketplace, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import AgentBlockStatus

from app.adapters.storage import get_storage
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.resolved_facts import facts_digest, for_rules
from app.repositories.uow import tenant_uow
from app.services.auto_fix.apply import auto_fix
from app.services.auto_fix.fixers import FixContext, FixRecord
from app.services.generation_context import GenerationContextError, build_context
from app.services.generation_images import load_context_images, order_for_listing, pick_context
from app.services.generation_persist import PersistPlan, Pins, make_persist
from app.services.generation_result import (
    Untrusted,
    check_result,
    map_outcome,
    recheck,
    salvage_records,
)
from app.services.pipeline_deps import SeamPortError, build_pipeline_deps
from app.services.rules_loader import RuleLoadError, load_rules
from app.workers import budget
from app.workers.errors import JobFailed
from app.workers.registry import JobContext, Outcome, register

log = get_logger(__name__)

# A model or embedding call that was rate limited, unavailable or slow: not the seller's to fix.
TRANSIENT_CODES: Final = frozenset(
    {ErrorCode.MODEL_UNAVAILABLE, ErrorCode.RATE_LIMITED, ErrorCode.TIMEOUT}
)
TRANSIENT_MESSAGE: Final = "The AI service was briefly unavailable. Try generating again."


def is_transient_failure(exc: BaseException) -> bool:
    """A retryable port refusal with a transient code; for an exception group, every leaf one."""
    if isinstance(exc, BaseExceptionGroup):
        return all(is_transient_failure(inner) for inner in exc.exceptions)
    return isinstance(exc, SeamPortError) and exc.retryable and exc.code in TRANSIENT_CODES


def _transient_record(record: AgentRunRecord) -> bool:
    return record.status == "timeout" or (
        record.status == "failed"
        and record.error_report is not None
        and record.error_report.error_code in TRANSIENT_CODES
    )


def runs_failed_transiently(
    records: Iterable[AgentRunRecord], blocks: Iterable[AgentBlockStatus] = ()
) -> bool:
    """Decided by each agent's last run, as a recovered 429 or timeout leaves a failed run
    behind: at least one agent ended on a failed run, every agent that ended failed ended on a
    transient one, and no agent with a failed block ended on a completed run (its output was
    refused, which a new try repeats)."""
    last: dict[str, AgentRunRecord] = {}
    for record in records:
        last[record.agent_name] = record
    ended_failed = [r for r in last.values() if r.status != "completed"]
    failed_blocks = {b.agent for b in blocks if b.status == "failed"}
    return (
        bool(ended_failed)
        and all(_transient_record(r) for r in ended_failed)
        and not any(last[a].status == "completed" for a in failed_blocks if a in last)
    )


def job_report(
    code: ErrorCode,
    message: str,
    *,
    ctx: JobContext,
    field: str = "listing",
    source: ErrorSource = ErrorSource.SYSTEM,
    attempts_used: int | None = None,
    max_attempts: int = 1,
) -> ErrorReport:
    meta = ERROR_CODE_META[code]
    retryable = meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT)
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=retryable,
        attempts_used=min(3, ctx.attempt if attempts_used is None else attempts_used),
        max_attempts=max(1, min(3, max_attempts)),
        errors=[
            ErrorItem(
                field=field,
                error_code=code,
                message=message,
                source=source,
                retryable=retryable,
                retry_class=meta.retry_class,
            )
        ],
        request_id=ctx.request_id,
    )


def _fail(ctx: JobContext, code: ErrorCode, message: str, **kw: Any) -> JobFailed:
    log.info("generation.failed", job_run_id=str(ctx.job_run_id), code=code.value)
    return JobFailed(job_report(code, message, ctx=ctx), **kw)


async def run_listing_generate(ctx: JobContext) -> Outcome:
    settings = get_settings()
    tenant, job_id = ctx.tenant_id, ctx.job_run_id
    try:
        reserved = await budget.reserve(tenant, job_id, ctx.attempt)
    except budget.BudgetRefused:
        raise _fail(ctx, ErrorCode.BUDGET_EXCEEDED, "The daily AI budget is used up.") from None
    if not reserved:  # the fence moved: a newer attempt owns the row; the wrapper drops the write
        raise _fail(ctx, ErrorCode.INTERNAL_ERROR, "generation failed")

    # ---- 2. preload, one short read transaction ----
    async with tenant_uow(tenant) as uow:
        job = await uow.job_runs.get_by_id(job_id)
        listing = (
            None
            if job is None or job.listing_id is None
            else await uow.listings.get(job.listing_id)
        )
        if job is None or listing is None:
            raise _fail(ctx, ErrorCode.INTERNAL_ERROR, "generation failed", zero_reservation=True)
        try:
            rules = await load_rules(
                uow, tenant_id=tenant, marketplace=listing.marketplace, category=listing.category
            )
        except RuleLoadError:
            raise _fail(
                ctx,
                ErrorCode.INTERNAL_ERROR,
                "The rules could not be loaded.",
                zero_reservation=True,
            ) from None
        facts = await uow.product_facts.resolve(listing.product_id)
        if facts is None:
            raise _fail(ctx, ErrorCode.NOT_FOUND, "The product is gone.", zero_reservation=True)
        try:
            gctx = await build_context(
                uow,
                job,
                listing,
                rules=rules,
                facts=facts,
                request_id=ctx.request_id,
                max_attempts=settings.llm_max_correction_attempts,
            )
        except GenerationContextError as exc:
            raise _fail(
                ctx, exc.code, "The generation context could not be built.", zero_reservation=True
            ) from None
        claim_started_at = job.started_at or datetime.now(UTC)
        created_by = job.created_by
        image_rows = await uow.images.listing_set(listing.product_id)
        critical = await uow.product_facts.critical_keys()  # SI4 A8/A9 (CP-D9)

    # SI1 T18 (PLAN D4): the product's images reach the pipeline as vision copies, read with no
    # transaction open; a missing one is skipped and logged by id
    context_images = await load_context_images(get_storage(), tenant, pick_context(image_rows))
    if context_images:
        gctx = gctx.model_copy(update={"images": context_images})

    product = for_rules(facts)
    pins = Pins(
        rules_version=rules.ruleset.rules_version,
        template_id=rules.template_id,
        template_version=rules.template_version,
        facts_digest=facts_digest(facts),
    )
    plan_base: dict[str, Any] = {
        "tenant_id": tenant,
        "job_id": job_id,
        "listing_id": listing.id,
        "marketplace": Marketplace(listing.marketplace),
        "request_id": ctx.request_id,
        "created_by": created_by,
        "pins": pins,
    }

    # ---- 3. the pipeline, no transaction open ----
    deps = await build_pipeline_deps(
        settings=settings,
        tenant_id=tenant,
        job_id=job_id,
        user_id=created_by,
        product_id=listing.product_id,
        marketplace=Marketplace(listing.marketplace),
        rules=rules,
        critical_keys=critical,
        product=product,
    )
    try:
        result = await run_listing_pipeline(gctx, deps)
    except asyncio.CancelledError:
        raise
    except TimeoutError:  # a model call passed its deadline (S26 AC 5): JOB_TIMEOUT, no version
        log.warning("generation.pipeline_timeout", job_run_id=str(job_id))
        plan = PersistPlan(**plan_base, move=None, rules_calls=deps.rules_calls)
        raise JobFailed(
            job_report(ErrorCode.JOB_TIMEOUT, "the model call passed its deadline", ctx=ctx),
            persist=make_persist(plan),
            zero_reservation=not deps.model_stage_reached,
        ) from None
    except Exception as exc:  # a port or the pipeline raised: recorded (step 5)
        transient = is_transient_failure(exc)
        log.warning(
            "generation.pipeline_error",
            job_run_id=str(job_id),
            error_class=type(exc).__name__,
            transient=transient,
            model_stage_reached=deps.model_stage_reached,
        )
        plan = PersistPlan(**plan_base, move=None, rules_calls=deps.rules_calls)
        failure = (
            job_report(
                ErrorCode.MODEL_UNAVAILABLE, TRANSIENT_MESSAGE, ctx=ctx, source=ErrorSource.AI_MODEL
            )
            if transient
            else job_report(ErrorCode.INTERNAL_ERROR, "generation failed", ctx=ctx)
        )
        raise JobFailed(
            failure, persist=make_persist(plan), zero_reservation=not deps.model_stage_reached
        ) from None
    finally:
        deps.close_spans()

    # ---- 4. trust, re-check, map, persist ----
    now = datetime.now(UTC)
    checked = check_result(result, ctx=gctx, claim_started_at=claim_started_at, now=now)
    if isinstance(checked, Untrusted):
        log.warning("generation.untrusted_result", job_run_id=str(job_id), check=checked.check)
        salvaged = salvage_records(result, gctx, claim_started_at=claim_started_at, now=now)
        plan = PersistPlan(
            **plan_base, move=None, rules_calls=deps.rules_calls, records=salvaged or ()
        )
        raise JobFailed(
            checked.report.model_copy(
                update={"attempts_used": min(3, ctx.attempt), "max_attempts": gctx.max_attempts}
            ),
            persist=make_persist(plan),
            zero_reservation=salvaged is not None,
        )
    trusted = checked
    if trusted.listing is not None:
        # CP15: the backend pins the version's images before the re-check, so the stored
        # re-check judged the images the version holds (a pipeline value is replaced)
        pinned = trusted.listing.model_copy(update={"images": order_for_listing(image_rows)})
        trusted = dataclasses.replace(trusted, listing=pinned)
    attempts_used = len(deps.rules_calls)
    if attempts_used != trusted.result.attempts_used:
        log.info(
            "generation.attempts_mismatch",
            job_run_id=str(job_id),
            seen=attempts_used,
            reported=trusted.result.attempts_used,
        )
    report: ErrorReport | None = None
    fixes: tuple[FixRecord, ...] = ()
    if trusted.listing is not None:
        # SI4 A9 (§2 fact 1, AC7): the same pure fix pass the correction loop ran, on the final
        # listing; the fixed listing is what the version stores. A human edit never gets here (D6)
        fixed = auto_fix(
            trusted.listing,
            lambda candidate: recheck(candidate, rules, product),
            FixContext(ruleset=rules.ruleset, template=rules.template, critical_keys=critical),
        )
        trusted = dataclasses.replace(trusted, listing=fixed.listing)
        report, fixes = fixed.report, fixed.records
    move = map_outcome(
        trusted.result.terminal_state, trusted.listing, report, trusted.result.blocks
    )
    if not move.saves_version:
        plan = PersistPlan(
            **plan_base, move=move, rules_calls=deps.rules_calls, records=trusted.records
        )
        transient = runs_failed_transiently(trusted.records, trusted.result.blocks)
        raise JobFailed(
            job_report(
                ErrorCode.MODEL_UNAVAILABLE if transient else ErrorCode.GENERATION_FAILED,
                TRANSIENT_MESSAGE if transient else "The pipeline produced no listing.",
                ctx=ctx,
                source=ErrorSource.AI_MODEL,
                attempts_used=max(1, attempts_used),
                max_attempts=gctx.max_attempts,
            ),
            persist=make_persist(plan),
            zero_reservation=True,
        )
    version_id = uuid.uuid4()
    outcome_result: dict[str, Any] = {"listing_version_id": str(version_id), "status": move.to}
    plan = PersistPlan(
        **plan_base,
        move=move,
        rules_calls=deps.rules_calls,
        records=trusted.records,
        listing=trusted.listing,
        image_reports=trusted.result.image_reports,
        image_reports_stored=deps.stored_image_reports,
        recheck=report,
        fixes=fixes,
        attempts_used=attempts_used,
        version_id=version_id,
        result=outcome_result,
    )
    error = None
    if move.to == "blocked" and report is not None:
        first = report.blocking[0].error_code if report.blocking else None
        error = report.model_copy(
            update={
                "error_code": report.error_code or first,
                "attempts_used": attempts_used,
                "max_attempts": gctx.max_attempts,
                "listing_id": listing.id,
                "request_id": ctx.request_id,
            }
        )
    return Outcome(
        "succeeded",
        outcome_result,
        persist=make_persist(plan),
        zero_reservation=True,
        error=error,
    )


def register_generation() -> None:
    register(JobKind.LISTING_GENERATE, run_listing_generate)
