"""The `GenerationContext` a `listing_generate` job hands the pipeline (S18 T11; D3-D6; CANON.md:118,
:125; DATABASE.md:806).

`build_context(uow, job, listing, rules=, facts=, request_id=, max_attempts=)` runs at claim (D4),
in the job's read transaction, from rows only (the AI package fetches nothing):

- `product` = `for_prompt(facts)`: parties reduced to `legal_name`, no GSTIN or contact (D5);
- `brand_profile` = `uow.brand_profile.for_prompt()` (D6, with `brand_description`);
- `marketplace_rules` = the loaded RuleSet; `category_template` = the current template's
  `for_generation()` (no adapter fields); no template → `CATEGORY_NOT_MAPPED` (D2);
- `previous_version` = the current version's `CanonicalListing`, or None;
- `user_instructions` = the `regenerate_requested` row's instructions when one names the job,
  else the job payload's (D3); `review_feedback` = the newest 5 reject feedbacks;
- `locale` = the brand's `content_locale`; `max_attempts` = the configured correction cap (1..3).

No marketplace account, token or settings value is read. The S2 secret walk runs on the built
context; a refusal is `VALIDATION_FAILED` and its text never reaches a log or a report.
"""

from __future__ import annotations

from typing import Any

from mkos_schemas.codes import ErrorCode
from mkos_schemas.jobs import GenerationContext
from mkos_schemas.listing import CanonicalListing
from pydantic import ValidationError

from app.core.logging import get_logger
from app.core.resolved_facts import ProductFacts, for_prompt
from app.db.models.listings import Listing, ListingVersion
from app.db.models.ops import JobRun
from app.repositories.uow import TenantUnitOfWork
from app.services.rules_loader import LoadedRules

log = get_logger(__name__)


class GenerationContextError(Exception):
    """The context cannot be built; `code` is the job's failure code. No row value is kept."""

    def __init__(self, code: ErrorCode, field: str) -> None:
        super().__init__(f"{code.value} at {field}")
        self.code = code
        self.field = field


def previous_listing(listing: Listing, version: ListingVersion | None) -> CanonicalListing | None:
    """The stored version as a `CanonicalListing`. A version stored as bare `ListingContent` (the
    demo seed's v1) is wrapped with the listing's ids; an unreadable one is None (logged)."""
    if version is None:
        return None
    stored: dict[str, Any] = dict(version.content_json or {})
    body: dict[str, Any] = stored if "content" in stored else {"content": stored}
    body.update(
        listing_id=str(listing.id),
        product_id=str(listing.product_id),
        marketplace=listing.marketplace,
        category=listing.category,
        version=version.version,
        locale=version.locale,
    )
    try:
        return CanonicalListing.model_validate(body)
    except ValidationError:
        log.warning("generation.previous_version_unreadable", version_id=str(version.id))
        return None


async def build_context(
    uow: TenantUnitOfWork,
    job: JobRun,
    listing: Listing,
    *,
    rules: LoadedRules,
    facts: ProductFacts,
    request_id: str,
    max_attempts: int,
) -> GenerationContext:
    if rules.template is None:
        raise GenerationContextError(ErrorCode.CATEGORY_NOT_MAPPED, "category")
    brand = await uow.brand_profile.for_prompt()
    previous = previous_listing(listing, await uow.listing_versions.current(listing))
    instructions: str | None
    if await uow.review_decisions.has_regenerate_for_job(job.id):
        instructions = await uow.review_decisions.instructions_for_job(job.id)
    else:
        raw = (job.payload_json or {}).get("user_instructions")
        instructions = raw if isinstance(raw, str) else None
    feedback = await uow.review_decisions.latest_reject_feedback(listing.id, 5)
    try:
        return GenerationContext.model_validate(
            {
                "request_id": request_id,
                "job_id": job.id,
                "tenant_id": job.tenant_id,
                "product_id": listing.product_id,
                "listing_id": listing.id,
                "marketplace": listing.marketplace,
                "category": listing.category,
                "product": for_prompt(facts),
                "brand_profile": brand,
                "marketplace_rules": rules.ruleset,
                "category_template": rules.template.for_generation(),
                "previous_version": previous,
                "user_instructions": instructions,
                "max_attempts": max(1, min(3, max_attempts)),
                "locale": brand.get("content_locale") or "en-IN",
                "review_feedback": feedback,
            }
        )
    except ValidationError:
        # the secret walk or a bound refused it; the error text may quote the input, so it is
        # dropped: only the class reaches the log
        log.warning("generation.context_refused", job_id=str(job.id), error="ValidationError")
        raise GenerationContextError(ErrorCode.VALIDATION_FAILED, "context") from None
