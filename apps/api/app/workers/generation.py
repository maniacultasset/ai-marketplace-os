"""The `listing_generate` job body (S18 T16; DW1-3; BACKEND-ARCHITECTURE.md:704-711).

The only module that imports `mkos_pipeline` (the S14b stub locally and in CI; the AI team's tagged
drop at deploy, T104). No session is open across the pipeline call. The steps:

1. `budget.reserve` (T7): the claim-time reservation against the tenant and global caps. A refusal
   → `JobFailed(BUDGET_EXCEEDED)`; the terminal hook moves the listing to
   `failed/generation_failed`.
2. One short read transaction: the job, the listing, `load_rules`, the resolved facts, then
   `build_context` (T11).
3. `build_pipeline_deps(..., product=for_rules(facts))` (CP13), then `run_listing_pipeline(ctx,
   deps)` with no transaction open; `deps.close_spans()` whatever happens.
4. `check_result` → `recheck` → `map_outcome` → `make_persist` → an `Outcome` (a version saved;
   the reservation zeroed, real spend replaces it) or a `JobFailed` (no version).
5. Any port or pipeline exception → `JobFailed(INTERNAL_ERROR)` with a fixed message (the class
   name is logged only), the recorded rules runs kept and the reservation kept (CP10).

Log lines carry ids and codes only: no instruction, product or model text.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from mkos_pipeline import run_listing_pipeline
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind, Marketplace
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.resolved_facts import facts_digest, for_rules
from app.repositories.uow import tenant_uow
from app.services.generation_context import GenerationContextError, build_context
from app.services.generation_persist import PersistPlan, Pins, make_persist
from app.services.generation_result import (
    Untrusted,
    check_result,
    map_outcome,
    recheck,
    salvage_records,
)
from app.services.pipeline_deps import build_pipeline_deps
from app.services.rules_loader import RuleLoadError, load_rules
from app.workers import budget
from app.workers.errors import JobFailed
from app.workers.registry import JobContext, Outcome, register

log = get_logger(__name__)


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
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=False,
        attempts_used=min(3, ctx.attempt if attempts_used is None else attempts_used),
        max_attempts=max(1, min(3, max_attempts)),
        errors=[
            ErrorItem(
                field=field,
                error_code=code,
                message=message,
                source=source,
                retryable=False,
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
            raise _fail(ctx, ErrorCode.INTERNAL_ERROR, "generation failed")
        try:
            rules = await load_rules(
                uow, tenant_id=tenant, marketplace=listing.marketplace, category=listing.category
            )
        except RuleLoadError:
            raise _fail(ctx, ErrorCode.INTERNAL_ERROR, "The rules could not be loaded.") from None
        facts = await uow.product_facts.resolve(listing.product_id)
        if facts is None:
            raise _fail(ctx, ErrorCode.NOT_FOUND, "The product is gone.")
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
            raise _fail(ctx, exc.code, "The generation context could not be built.") from None
        claim_started_at = job.started_at or datetime.now(UTC)
        created_by = job.created_by

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
        product=product,
    )
    try:
        result = await run_listing_pipeline(gctx, deps)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a port or the pipeline raised: recorded, the reservation kept
        log.warning(
            "generation.pipeline_error", job_run_id=str(job_id), error_class=type(exc).__name__
        )
        plan = PersistPlan(**plan_base, move=None, rules_calls=deps.rules_calls)
        raise JobFailed(
            job_report(ErrorCode.INTERNAL_ERROR, "generation failed", ctx=ctx),
            persist=make_persist(plan),
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
    attempts_used = len(deps.rules_calls)
    if attempts_used != trusted.result.attempts_used:
        log.info(
            "generation.attempts_mismatch",
            job_run_id=str(job_id),
            seen=attempts_used,
            reported=trusted.result.attempts_used,
        )
    report = None if trusted.listing is None else recheck(trusted.listing, rules, product)
    move = map_outcome(
        trusted.result.terminal_state, trusted.listing, report, trusted.result.blocks
    )
    if not move.saves_version:
        plan = PersistPlan(
            **plan_base, move=move, rules_calls=deps.rules_calls, records=trusted.records
        )
        raise JobFailed(
            job_report(
                ErrorCode.GENERATION_FAILED,
                "The pipeline produced no listing.",
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
        recheck=report,
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
