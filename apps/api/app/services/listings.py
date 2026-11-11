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
from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.validation import ValidationResult, ValidationSummary

from app.core.errors import NotFoundError
from app.core.resolved_facts import for_rules, listing_facts_digest
from app.repositories.uow import TenantUnitOfWork
from app.services.generation_persist import Pins
from app.services.generation_result import recheck
from app.services.listing_display import display_fields
from app.services.rules_engine import is_recommendation
from app.services.rules_loader import load_rules
from app.services.seller_facts import seller_overlay

BLOCKED_REASON: Final = "validation_errors"


@dataclass(frozen=True, slots=True)
class Check:
    report: ErrorReport
    pins: Pins
    template: CategoryTemplate | None = None  # SI9: the questions' template subset


def to_detail(
    listing: Any,
    content: CanonicalListing | None,
    summary: ValidationSummary | None,
    needs_you: int = 0,
) -> dict[str, Any]:
    """The `ListingDetail` fields (the router validates them into the response model)."""
    return {**listing_fields(listing, needs_you), "content": content, "validation": summary}


def listing_fields(listing: Any, needs_you: int = 0) -> dict[str, Any]:
    """The `ListingOut` fields of a `listings` row (S19's list reuses them); `needs_you` is its
    open blocking issues on the current version (`listing_display`)."""
    return {
        **display_fields(listing, needs_you),
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
        "external_id": getattr(listing, "external_id", None),
        "status_changed_at": listing.status_changed_at,
        "created_at": listing.created_at,
        "updated_at": listing.updated_at,
        "remote_check": remote_check(listing),
    }


def remote_check(listing: Any) -> dict[str, Any] | None:
    """SI11: the `RemoteCheckOut` fields of the marketplace dry run, or None when none ran."""
    version_id = getattr(listing, "remote_check_version_id", None)
    if version_id is None:
        return None
    return {
        "version_id": version_id,
        "status": listing.remote_check_status,
        "at": listing.remote_check_at,
    }


async def current_pins(uow: TenantUnitOfWork, listing: Any) -> Pins:
    """The pins a run must carry to count now (DATABASE.md:823, G9): the currently resolved
    rules version and template, and the digest of the product's current facts. No engine run."""
    rules = await load_rules(
        uow, tenant_id=listing.tenant_id, marketplace=listing.marketplace, category=listing.category
    )
    family = await uow.product_facts.resolve_family(listing.product_id)
    if family is None:
        raise NotFoundError("product_id")
    return Pins(
        rules_version=rules.ruleset.rules_version,
        template_id=rules.template_id,
        template_version=rules.template_version,
        facts_digest=listing_facts_digest(*family),  # SI2 D9: a family pins its children too
    )


async def rules_check(uow: TenantUnitOfWork, listing: Any, canonical: CanonicalListing) -> Check:
    # a RuleLoadError is a server fault (a stored rule broke): it surfaces as 500, never a pass
    rules = await load_rules(
        uow, tenant_id=listing.tenant_id, marketplace=listing.marketplace, category=listing.category
    )
    family = await uow.product_facts.resolve_family(listing.product_id)
    if family is None:
        raise NotFoundError("product_id")
    facts = family[0]
    rejections = await uow.products.photo_rejections(listing.product_id)  # SI9 B5
    # SI2 T16 (D5): the engine judges the seller's factual values, never the AI's
    report = recheck(
        seller_overlay(canonical, facts, rules.template),
        rules,
        for_rules(facts),
        rejections=rejections,
    )
    return Check(
        template=rules.template,
        report=report,
        pins=Pins(
            rules_version=rules.ruleset.rules_version,
            template_id=rules.template_id,
            template_version=rules.template_version,
            facts_digest=listing_facts_digest(*family),
        ),
    )


def human_move(report: ErrorReport, adapter: ErrorReport | None = None) -> tuple[str, str | None]:
    """The status a human check point moves to. SI9 B4: a blocking marketplace check blocks as
    a Rules block does; its warnings change nothing (CP-D3)."""
    if report.blocking or (adapter is not None and adapter.blocking):
        return "blocked", BLOCKED_REASON
    # feedback item 10: a missing recommended attribute alone never moves (or un-approves) a
    # listing; the review screen lists it under "Recommended to add"
    if any(not is_recommendation(item) for item in report.errors):
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
    cause: str | None = None,
) -> Any:
    """One `rules_engine` run linked to `version_id`; with a `cause`, the listing's issues are
    synced from it in the same transaction (S19 T18, CP-D2)."""
    run = await uow.validation_runs.insert(
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
    if cause is not None:
        from app.services.issues import sync_issues

        await sync_issues(
            uow,
            listing,
            "rules_engine",
            check.report,
            run_id=run.id,
            cause=cause,
            version_id=version_id,
            critical_keys=await uow.product_facts.critical_keys(),
        )
    return run
