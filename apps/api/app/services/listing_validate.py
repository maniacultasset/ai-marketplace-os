"""Validate a listing version on demand (S18 T24; D14; CP4, CP7; DATABASE.md:1384).

`validate_listing(uow, principal, listing_id, version_id)`, under the listing's row lock (two
concurrent validates with equal pins write one run):

1. the version: the given one (it must belong to the listing, else 404 on
   `listing_version_id`) or the current one (none → 404, CP7);
2. the Rules Engine with the current rules, template and facts. A run with equal pins already
   stored for the version → its summary, nothing written; else one `rules_engine` run;
3. on the current version, when the outcome's status differs: `revalidate` from `validated`,
   `needs_review`, `approved` (to a non-clean status, voiding the approval) and `blocked` only
   when its reason is `validation_errors` or `corrections_exhausted` (CP4: a human rejection is
   never undone). Anything else: the run only.
4. always 200: a failed check is `passed: false`, never an error.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from mkos_schemas.validation import ValidationSummary

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.listings import UserActor
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.repositories.validation_runs import summary_of
from app.services.generation_context import previous_listing
from app.services.listings import human_move, insert_run, rules_check

log = get_logger(__name__)
MOVABLE: Final = frozenset({"validated", "needs_review", "approved", "blocked"})
BLOCKED_REVALIDATE: Final = frozenset({"validation_errors", "corrections_exhausted"})


def _target(listing: Any, to: str) -> str | None:
    """The status a revalidate moves to, or None (run only)."""
    status = listing.status
    if status not in MOVABLE or listing.paused_at is not None or listing.archived_at is not None:
        return None
    if status == "blocked" and listing.status_reason not in BLOCKED_REVALIDATE:
        return None
    if status == "approved" and to == "validated":
        return None  # a clean re-check keeps the approval
    if status == to:
        return None
    return to


async def validate_listing(
    uow: TenantUnitOfWork,
    principal: Principal,
    listing_id: uuid.UUID,
    version_id: uuid.UUID | None,
) -> ValidationSummary:
    listing = await uow.listings.get_for_update(listing_id)
    if listing is None:
        raise NotFoundError("listing_id")
    wanted = version_id or listing.current_version_id
    version = None if wanted is None else await uow.listing_versions.get(listing.id, wanted)
    canonical = previous_listing(listing, version)
    if version is None or canonical is None:
        raise NotFoundError("listing_version_id")
    check = await rules_check(uow, listing, canonical)
    pins = check.pins
    same = await uow.validation_runs.find_equal_pins(
        version.id, pins.rules_version, pins.template_id, pins.template_version, pins.facts_digest
    )
    if same is not None:
        return summary_of([same])
    current = version.id == listing.current_version_id
    # only the current version's issues are the listing's inbox (a past version's run is history)
    run = await insert_run(uow, listing, version.id, check, cause="revalidate" if current else None)
    to, reason = human_move(check.report)
    target = _target(listing, to) if version.id == listing.current_version_id else None
    if target is not None:
        await uow.listings.move(
            listing,
            to=target,
            event="revalidate",
            reason=reason,
            actor=UserActor(principal.user_id),
            clear_approval=listing.status == "approved",
        )
        log.info("listing.revalidated", listing_id=str(listing.id), status=target)
    return summary_of([run])


async def validate(
    principal: Principal, listing_id: uuid.UUID, version_id: uuid.UUID | None
) -> ValidationSummary:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await validate_listing(uow, principal, listing_id, version_id)
