"""Listing reads and the human re-check shared by edit and validate (S18 T23, T24; S19 reuses
`to_detail`).

- `to_detail(listing, content, summary)`: the `ListingDetail` fields.
- `rules_check(uow, listing, canonical)`: the Rules Engine on a listing with the current rules,
  template and product facts (inv 2), returning the report and the pins a `validation_runs` row
  carries (D7).
- `human_move(report)`: a report's status for a human-triggered check: a blocking item →
  `blocked/validation_errors`, any other item → `needs_review`, none → `validated`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.enums import ErrorSource, Marketplace
from mkos_schemas.errors import ErrorReport
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.validation import ValidationResult, ValidationSummary

from app.core.errors import NotFoundError
from app.core.resolved_facts import facts_digest, for_rules
from app.repositories.uow import TenantUnitOfWork
from app.services.generation_persist import Pins
from app.services.generation_result import recheck
from app.services.rules_loader import load_rules

BLOCKED_REASON: Final = "validation_errors"


@dataclass(frozen=True, slots=True)
class Check:
    report: ErrorReport
    pins: Pins


def to_detail(
    listing: Any, content: CanonicalListing | None, summary: ValidationSummary | None
) -> dict[str, Any]:
    """The `ListingDetail` fields (the router validates them into the response model)."""
    return {
        "id": listing.id,
        "product_id": listing.product_id,
        "marketplace": listing.marketplace,
        "status": listing.status,
        "status_reason": listing.status_reason,
        "marketplace_state": listing.marketplace_state,
        "category": listing.category,
        "current_version_id": listing.current_version_id,
        "approved_version_id": listing.approved_version_id,
        "live_url": getattr(listing, "live_url", None),
        "status_changed_at": listing.status_changed_at,
        "created_at": listing.created_at,
        "updated_at": listing.updated_at,
        "content": content,
        "validation": summary,
    }


async def rules_check(uow: TenantUnitOfWork, listing: Any, canonical: CanonicalListing) -> Check:
    # a RuleLoadError is a server fault (a stored rule broke): it surfaces as 500, never a pass
    rules = await load_rules(
        uow, tenant_id=listing.tenant_id, marketplace=listing.marketplace, category=listing.category
    )
    facts = await uow.product_facts.resolve(listing.product_id)
    if facts is None:
        raise NotFoundError("product_id")
    report = recheck(canonical, rules, for_rules(facts))
    return Check(
        report=report,
        pins=Pins(
            rules_version=rules.ruleset.rules_version,
            template_id=rules.template_id,
            template_version=rules.template_version,
            facts_digest=facts_digest(facts),
        ),
    )


def human_move(report: ErrorReport) -> tuple[str, str | None]:
    if report.blocking:
        return "blocked", BLOCKED_REASON
    if report.errors:
        return "needs_review", None
    return "validated", None


async def insert_run(
    uow: TenantUnitOfWork,
    listing: Any,
    version_id: Any,
    check: Check,
    *,
    attempt: int = 1,
    request_id: str | None = None,
) -> Any:
    return await uow.validation_runs.insert(
        ValidationResult(
            tenant_id=listing.tenant_id,
            listing_id=listing.id,
            listing_version_id=version_id,
            marketplace=Marketplace(listing.marketplace),
            source=ErrorSource.RULES_ENGINE,
            passed=not check.report.blocking,
            attempt=attempt,
            rules_version=check.pins.rules_version,
            duration_ms=0,
            report=check.report,
            created_at=datetime.now(UTC),
        ),
        template_id=check.pins.template_id,
        template_version=check.pins.template_version,
        facts_digest=check.pins.facts_digest,
        request_id=request_id,
    )
