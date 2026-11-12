"""The auto-fix body of `listing_generate` (SI11 T15; ADR-0028 proposed; P:B5 part 1, D3; CP-D17).

A job with `payload_json.mode == "auto_fix"` (`AutoFixPayload`) fixes the marketplace's content
refusals of one base version with no model call:

1. one short read transaction: the job, the listing (still `generating` on this job, the base
   still current), the open refusals it names, the rules, the resolved facts;
2. the deterministic content fix (`resolution.content.prepare`: SI4's trim against the
   marketplace's limit) on the base version's content; nothing fixed -> `JobFailed
   GENERATION_FAILED` ("Kuinn could not fix it"); the generate terminal hook moves the listing
   to `failed/generation_failed` (the live version stays);
3. the full Rules Engine re-check of the fixed content; a blocking item -> `blocked` (the
   seeded `corrections_exhausted` move), else `needs_review`: a Kuinn-made change always waits
   for a person;
4. the persist (`make_persist`): a `regeneration` version whose parent is the base and whose
   `changed_paths` are the fixed paths only (so `lv_guard`'s contributor chain refuses the base
   generator, who is this version's maker, as approver), the refusals closed `deterministic_fix`
   with the trail text, and the dry run asked again with `trigger resolution`.

Client feedback 14: a fix on a path the seller locked is dropped and the locked paths stay the
base's (`sections.keep_locked`); nothing left -> "Kuinn could not fix it". The planner already
gives a locked path to the seller, so this is the backstop.

The reservation is zeroed on every path (no model call). The fix version is never resent on its
own: a non-maker approves it and publishes ("Approve and resend").
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace

from app.core.logging import get_logger
from app.core.resolved_facts import for_rules, listing_facts_digest
from app.repositories.uow import tenant_uow
from app.services.generation_context import previous_listing
from app.services.generation_persist import PersistPlan, Pins, make_persist
from app.services.generation_result import BLOCKED, NEEDS_REVIEW, recheck
from app.services.resolution.content import prepare
from app.services.rules_loader import RuleLoadError, load_rules
from app.services.sections import covered, keep_locked
from app.workers.errors import JobFailed
from app.workers.payloads import AutoFixPayload
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)

GENERATING: Final = "generating"
NOT_FIXED: Final = "Kuinn could not fix it."


def _fail(ctx: JobContext, code: ErrorCode, message: str) -> JobFailed:
    from app.workers.generation import job_report

    log.info("auto_fix.failed", job_run_id=str(ctx.job_run_id), code=code.value)
    return JobFailed(job_report(code, message, ctx=ctx), zero_reservation=True)


async def run_auto_fix(ctx: JobContext) -> Outcome:
    payload = AutoFixPayload.model_validate(ctx.payload)
    tenant, job_id = ctx.tenant_id, ctx.job_run_id
    async with tenant_uow(tenant) as uow:
        job = await uow.job_runs.get_by_id(job_id)
        listing = await uow.listings.get(payload.listing_id)
        if job is None or listing is None or job.listing_id != listing.id:
            raise _fail(ctx, ErrorCode.INTERNAL_ERROR, "auto-fix failed")
        if (
            listing.status != GENERATING
            or listing.current_version_id != payload.base_version_id
            or await uow.listings.latest_generate_job_id(listing.id) != job_id
        ):
            raise _fail(ctx, ErrorCode.GENERATION_FAILED, NOT_FIXED)
        base = await uow.listing_versions.get(listing.id, payload.base_version_id)
        rows = await uow.listing_issues.open_by_ids(listing.id, list(payload.issue_ids))
        found = None if base is None else await prepare(uow, listing, base, rows)
        if base is None or found is None or not found[1]:
            raise _fail(ctx, ErrorCode.GENERATION_FAILED, NOT_FIXED)
        try:
            rules = await load_rules(
                uow, tenant_id=tenant, marketplace=listing.marketplace, category=listing.category
            )
        except RuleLoadError:
            raise _fail(ctx, ErrorCode.INTERNAL_ERROR, "The rules could not be loaded.") from None
        family = await uow.product_facts.resolve_family(listing.product_id)
        if family is None:
            raise _fail(ctx, ErrorCode.NOT_FOUND, "The product is gone.")
        rejections = await uow.products.photo_rejections(listing.product_id)
        created_by = base.generated_by_user_id
        before = previous_listing(listing, base)
    fixed, fixes = found
    locked = tuple(listing.locked_paths or ())
    if locked:  # client feedback 14: the planner never sends a locked path; never rewrite one
        fixes = tuple(f for f in fixes if not covered(f.record.field, locked))
        if not fixes or before is None:
            raise _fail(ctx, ErrorCode.GENERATION_FAILED, NOT_FIXED)
        fixed = keep_locked(fixed, before, locked)
    report = recheck(fixed, rules, for_rules(family[0]), rejections=rejections)
    move = BLOCKED if report.blocking else NEEDS_REVIEW
    version_id = uuid.uuid4()
    paths = tuple(sorted({f.record.field for f in fixes}))
    result: dict[str, Any] = {
        "listing_version_id": str(version_id),
        "status": move.to,
        "mode": "auto_fix",
        "changed_paths": list(paths),
    }
    plan = PersistPlan(
        tenant_id=tenant,
        job_id=job_id,
        listing_id=listing.id,
        marketplace=Marketplace(listing.marketplace),
        request_id=ctx.request_id,
        created_by=created_by,
        move=move,
        pins=Pins(
            rules_version=rules.ruleset.rules_version,
            template_id=rules.template_id,
            template_version=rules.template_version,
            facts_digest=listing_facts_digest(*family),
        ),
        listing=fixed,
        recheck=report,
        version_id=version_id,
        result=result,
        template=rules.template,
        preview_trigger="resolution",
        origin="regeneration",
        changed_paths=paths,
        closes=tuple((f.issue_id, f.record.action_taken) for f in fixes),
    )
    log.info(
        "auto_fix.fixed",
        job_run_id=str(job_id),
        listing_id=str(listing.id),
        fixes=len(fixes),
        status=move.to,
    )
    return Outcome("succeeded", result, persist=make_persist(plan), zero_reservation=True)
