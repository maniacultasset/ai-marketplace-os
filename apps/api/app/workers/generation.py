"""The `listing_generate` job body (S18 T16; DW1-3; BACKEND-ARCHITECTURE.md:704-711).

The only module that imports `mkos_pipeline` (the S14b stub locally and in CI; the AI team's tagged
drop at deploy, T104). No session is open across the pipeline call. The steps:

1. `budget.reserve` (T7): the claim-time reservation against the AI credit (SI10), the tenant and
   the global caps. A refusal → `JobFailed(BUDGET_EXCEEDED)`, or the pause's credit code; the
   terminal hook moves the listing to `failed/generation_failed`.
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
   reservation is zeroed on every terminal path (SI10 D6; DATABASE.md "Budget reservation"): the
   key's credit limit is the backstop for the few unrecorded calls a timeout or exception loses.
6. SI10 (D5, CP-D15): a provider account refusal (a query-embed 402, a failed record with
   `AI_CREDIT_EXHAUSTED` or the legacy `BUDGET_EXCEEDED`) pauses AI for everyone and fails with
   `AI_CREDIT_EXHAUSTED`; a key suspicion (a wire 401/403, a 0.3.2 `AI_PROVIDER_KEY_INVALID`
   record) is confirmed with OpenRouter's `/key` first: refused → pause `key_invalid` and
   `AI_PROVIDER_KEY_INVALID`; else this job alone fails `MODEL_UNAVAILABLE`, not retryable.

SI3 (photo facts): the read transaction also reads the product's newest recorded photo set; after
the vision copies load, `build_targets` decides the `photo_targets` (none when the photo set is
unchanged, so a second marketplace or a regeneration on the same photos makes no vision call).
A trusted result's observations are screened (`screen`) and the decision rides on the persist
plan of both the saved and the no-version path; the untrusted, timeout and exception paths carry
none, so they record no photo set and the next run tries again. A failed photo call never fails
generation (`runs_failed_transiently` ignores photo records). The claim-time reservation covers
the vision call.

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
from app.core.resolved_facts import for_rules, listing_facts_digest
from app.repositories.uow import tenant_uow
from app.services.ai_credit import REASON_CODE, EmbedRefusal, classify_refusal
from app.services.auto_fix.apply import auto_fix
from app.services.auto_fix.fixers import FixContext, FixRecord
from app.services.budget import CREDIT_MESSAGES
from app.services.generation_context import GenerationContextError, build_context
from app.services.generation_images import load_context_images, order_for_listing, pick_context
from app.services.generation_persist import PersistPlan, Pins, make_persist
from app.services.generation_result import (
    NEEDS_REVIEW,
    Untrusted,
    check_result,
    map_outcome,
    recheck,
    salvage_records,
)
from app.services.photo_facts import (
    PHOTO_PROMPT_PREFIX,
    build_targets,
    photo_read_skip,
    photo_set,
    screen,
)
from app.services.pipeline_deps import SeamPortError, build_pipeline_deps
from app.services.resolution.merge import merge_targets
from app.services.rules_loader import RuleLoadError, load_rules
from app.services.seller_facts import seller_overlay
from app.workers import budget
from app.workers.errors import JobFailed
from app.workers.payloads import AutoFixPayload
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
        if (record.prompt_version or "").startswith(PHOTO_PROMPT_PREFIX):
            continue  # SI3: a failed photo call never fails generation
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


REFUSED_MESSAGE: Final = "The AI service refused this request."


def _refused_not_retryable(ctx: JobContext) -> ErrorReport:
    """`MODEL_UNAVAILABLE` for this job only, not retryable (an unconfirmed 401/403)."""
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.MODEL_UNAVAILABLE,
        retryable=False,
        attempts_used=min(3, ctx.attempt),
        max_attempts=1,
        errors=[
            ErrorItem(
                field="listing",
                error_code=ErrorCode.MODEL_UNAVAILABLE,
                message=REFUSED_MESSAGE,
                source=ErrorSource.SYSTEM,
                retryable=False,
            )
        ],
        request_id=ctx.request_id,
    )


def _refusal_of(exc: BaseException) -> EmbedRefusal | None:
    """A credit refusal raised through a port (an exception group's leaves included)."""
    if isinstance(exc, BaseExceptionGroup):
        found = [_refusal_of(inner) for inner in exc.exceptions]
        return "credit" if "credit" in found else None
    if isinstance(exc, SeamPortError) and exc.code is ErrorCode.AI_CREDIT_EXHAUSTED:
        return "credit"
    return None


async def _refusal_failure(ctx: JobContext, refusal: EmbedRefusal, **kw: Any) -> JobFailed:
    """The one outcome of a provider account refusal (module docstring, step 6)."""
    job_id = ctx.job_run_id
    if refusal == "credit":
        await budget.pause_ai("credit_exhausted", job_id=job_id)
        report = job_report(
            ErrorCode.AI_CREDIT_EXHAUSTED, CREDIT_MESSAGES["credit_exhausted"], ctx=ctx
        )
    elif await budget.confirm_key_refusal():
        await budget.pause_ai("key_invalid", job_id=job_id)
        report = job_report(
            ErrorCode.AI_PROVIDER_KEY_INVALID, CREDIT_MESSAGES["key_invalid"], ctx=ctx
        )
    else:
        report = _refused_not_retryable(ctx)
    log.info("generation.failed", job_run_id=str(job_id), code=str(report.error_code))
    return JobFailed(report, zero_reservation=True, **kw)


async def run_listing_generate(ctx: JobContext) -> Outcome:
    if ctx.payload.get("mode") == "auto_fix":  # SI11 T15: the deterministic content fix
        from app.workers.auto_fix_job import run_auto_fix

        return await run_auto_fix(ctx)
    # SI11 T16: an AI rewrite of the refused fields only (the same body, gates and records)
    fix = (
        AutoFixPayload.model_validate(ctx.payload)
        if ctx.payload.get("mode") == "ai_field"
        else None
    )
    settings = get_settings()
    tenant, job_id = ctx.tenant_id, ctx.job_run_id
    try:
        reserved = await budget.reserve(tenant, job_id, ctx.attempt)
    except budget.BudgetRefused as refused:
        if refused.scope == "credit" and refused.reason is not None:
            raise _fail(
                ctx,
                REASON_CODE[refused.reason],
                CREDIT_MESSAGES[refused.reason],
                zero_reservation=True,
            ) from None
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
        if fix is not None and listing.current_version_id != fix.base_version_id:
            raise _fail(
                ctx, ErrorCode.GENERATION_FAILED, "Kuinn could not fix it.", zero_reservation=True
            )
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
        family = await uow.product_facts.resolve_family(listing.product_id)
        if family is None:
            raise _fail(ctx, ErrorCode.NOT_FOUND, "The product is gone.", zero_reservation=True)
        facts = family[0]
        try:
            gctx = await build_context(
                uow,
                job,
                listing,
                rules=rules,
                facts=facts,
                request_id=ctx.request_id,
                max_attempts=settings.llm_max_correction_attempts,
                target_fields=() if fix is None else fix.target_fields,
                feedback=() if fix is None else fix.feedback,
            )
        except GenerationContextError as exc:
            raise _fail(
                ctx, exc.code, "The generation context could not be built.", zero_reservation=True
            ) from None
        claim_started_at = job.started_at or datetime.now(UTC)
        created_by = job.created_by
        image_rows = await uow.images.listing_set(listing.product_id)
        critical = await uow.product_facts.critical_keys()  # SI4 A8/A9 (CP-D9)
        seen_set = await uow.job_runs.latest_photo_set(listing.product_id)
        rejections = await uow.products.photo_rejections(listing.product_id)  # SI9 B5
        # SI11 T16 (backstop): only a refusal on a path the model was sent closes as its fix
        covered = (
            ()
            if fix is None
            else tuple(
                r.id
                for r in await uow.listing_issues.open_by_ids(listing.id, fix.issue_ids)
                if r.field_path in fix.target_fields
            )
        )

    # SI1 T18 (PLAN D4): the product's images reach the pipeline as vision copies, read with no
    # transaction open; a missing one is skipped and logged by id
    picked = pick_context(image_rows)
    context_images = await load_context_images(get_storage(), tenant, picked)
    if context_images:
        gctx = gctx.model_copy(update={"images": context_images})
    # SI3: the keys to read from the photos (none when the photo set is the last one recorded)
    sent_set = photo_set(context_images, {r.image_id: r.clean_content_hash or "" for r in picked})
    targets = (
        []
        if rules.template is None or fix is not None  # a field fix reads no photos
        else build_targets(
            facts, rules.template, context_images, current_set=sent_set, last_set=seen_set
        )
    )
    if targets:
        gctx = gctx.model_copy(update={"photo_targets": targets})
    # SI9 B7: the read status the saved version records (`read` until the persist knows more)
    skip = (
        "not_read"
        if rules.template is None
        else photo_read_skip(
            facts, rules.template, context_images, current_set=sent_set, last_set=seen_set
        )
    )
    photo_read = {"status": skip or ("read" if targets else "not_read"), "written": 0, "asked": 0}

    product = for_rules(facts)
    pins = Pins(
        rules_version=rules.ruleset.rules_version,
        template_id=rules.template_id,
        template_version=rules.template_version,
        facts_digest=listing_facts_digest(*family),  # SI2 D9: a family pins its children too
    )
    plan_base: dict[str, Any] = {
        "template": rules.template,  # SI9 CP-D23
        "tenant_id": tenant,
        "job_id": job_id,
        "listing_id": listing.id,
        "marketplace": Marketplace(listing.marketplace),
        "request_id": ctx.request_id,
        "created_by": created_by,
        "pins": pins,
    }
    if fix is not None:  # SI11 T16: only the target paths change; the refusals close as AI fixes
        plan_base.update(
            origin="regeneration",
            changed_paths=tuple(fix.target_fields),
            closes=tuple(
                (issue_id, "Kuinn rewrote this field the way the marketplace accepts it.")
                for issue_id in covered
            ),
            close_resolution="ai_correction",
            preview_trigger="resolution",
        )

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
        rejections=rejections,  # SI9 B5 (CP-D13): the port's ERROR flags
        product=product,
        facts=facts,
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
            zero_reservation=True,  # SI10 D6: every terminal path releases the hold
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
        refusal = deps.embed_refusal or _refusal_of(exc)
        if refusal is not None:
            raise await _refusal_failure(ctx, refusal, persist=make_persist(plan)) from None
        failure = (
            job_report(
                ErrorCode.MODEL_UNAVAILABLE, TRANSIENT_MESSAGE, ctx=ctx, source=ErrorSource.AI_MODEL
            )
            if transient
            else job_report(ErrorCode.INTERNAL_ERROR, "generation failed", ctx=ctx)
        )
        raise JobFailed(failure, persist=make_persist(plan), zero_reservation=True) from None
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
        # SI2 T16 (D5): the version stores, and the re-check judges, the seller's factual values
        pinned = seller_overlay(pinned, facts, rules.template)
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
    if trusted.listing is not None and fix is not None and gctx.previous_version is not None:
        # SI11 T16: the base everywhere but the targets, whatever the model returned
        merged = merge_targets(gctx.previous_version, trusted.listing, fix.target_fields)
        trusted = dataclasses.replace(trusted, listing=merged)
        report = recheck(merged, rules, product, rejections=rejections)
    elif trusted.listing is not None:
        # SI4 A9 (§2 fact 1, AC7): the same pure fix pass the correction loop ran, on the final
        # listing; the fixed listing is what the version stores. A human edit never gets here (D6)
        fixed = auto_fix(
            trusted.listing,
            lambda candidate: recheck(candidate, rules, product, rejections=rejections),
            FixContext(ruleset=rules.ruleset, template=rules.template, critical_keys=critical),
        )
        trusted = dataclasses.replace(trusted, listing=fixed.listing)
        report, fixes = fixed.report, fixed.records
    move = map_outcome(
        trusted.result.terminal_state, trusted.listing, report, trusted.result.blocks
    )
    if fix is not None and move.to == "validated":
        move = NEEDS_REVIEW  # a Kuinn-made change always waits for a person
    photo: dict[str, Any] = {}
    if targets and rules.template is not None:  # SI3: only a trusted result, this run's targets
        decision = screen(
            trusted.result.photo_observations,
            trusted.records,
            targets=targets,
            images=context_images,
            facts=facts,
            required=frozenset(rules.template.required_attributes if rules.template else ()),
            rejected=rejections,
            template=rules.template,
        )
        log.info(
            "generation.photo_screen",
            job_run_id=str(job_id),
            writes=len(decision.writes),
            tasks=len(decision.tasks),
            dropped=[reason for _key, reason in decision.dropped],
        )
        photo = {
            "photo": decision,
            "pre_facts": facts,
            "pre_children": family[1],
            "seen_set": seen_set,
            "sent_set": sent_set,
        }
    if not move.saves_version:
        failed_result: dict[str, Any] = {}
        plan = PersistPlan(
            **plan_base,
            move=move,
            rules_calls=deps.rules_calls,
            records=trusted.records,
            result=failed_result,
            **photo,
        )
        refusal = classify_refusal(trusted.records, deps.embed_refusal)
        if refusal is not None:
            raise await _refusal_failure(
                ctx, refusal, persist=make_persist(plan), result=failed_result
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
            result=failed_result,
        )
    version_id = uuid.uuid4()
    outcome_result: dict[str, Any] = {
        "listing_version_id": str(version_id),
        "status": move.to,
        "photo_read": photo_read,
    }
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
        **photo,
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
