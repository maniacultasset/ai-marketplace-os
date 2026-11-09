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
4. on the current version, SI4 A13's questions (`ask_after_check`), then the
   `human_task_opened` move when one opened and the status allows it;
4a. SI9 B5: equal pins with different "facts changed, regenerate" items (a photo value was
   rejected since the stored run) write a new run, as different pins do;
4b. SI9 B4 (CP-D4): on the current version, the marketplace check runs before the equal-pins
   return (the stored definition is not a pin). Its items sync under `schema` and block as a
   Rules block does. With equal pins, a check whose blocking set equals the open `schema` error
   rows writes nothing; a different one syncs `schema`, takes the `revalidate` move and opens
   the questions, and writes no validation run;
4c. SI11 fix round 2: with equal pins and an open pre-check ERROR on the current version (the
   facts were changed and changed back), the marketplace's dry run is asked again;
5. always 200: a failed check is `passed: false`, never an error.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorReport
from mkos_schemas.validation import ValidationSummary

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.listings import UserActor
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.repositories.validation_runs import summary_of
from app.services.adapter_check import adapter_check
from app.services.generation_context import previous_listing
from app.services.issue_questions import ask_after_check
from app.services.issues import dedupe_key, sync_issues
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
    *,
    on_reject: bool = False,
) -> ValidationSummary:
    """`on_reject` (SI9 B5): the photo-reject re-check moves an `approved` listing only on an
    ERROR, since a WARNING allows approval."""
    listing = await uow.listings.get_for_update(listing_id)
    if listing is None:
        raise NotFoundError("listing_id")
    wanted = version_id or listing.current_version_id
    version = None if wanted is None else await uow.listing_versions.get(listing.id, wanted)
    canonical = previous_listing(listing, version)
    if version is None or canonical is None:
        raise NotFoundError("listing_version_id")
    check = await rules_check(uow, listing, canonical)
    current = version.id == listing.current_version_id
    adapter = await adapter_check(uow, listing.id) if current else None
    pins = check.pins
    same = await uow.validation_runs.find_equal_pins(
        version.id, pins.rules_version, pins.template_id, pins.template_version, pins.facts_digest
    )
    if same is not None and same_flags(same, check.report):
        if adapter is not None and await schema_changed(uow, listing, adapter):
            await _sync_schema(uow, listing, version.id, adapter)
            listing = await _revalidate(uow, principal, listing, check, adapter, on_reject)
            await ask_after_check(
                uow,
                listing,
                UserActor(principal.user_id),
                canonical.images,
                template=check.template,
            )
        elif current:
            # fix rounds 2-3: facts restored to ones already judged (A -> B -> A -> B) bring
            # back equal pins, yet the newest pre-check judged other facts. request_preview
            # replays only a verdict on these very inputs, else asks the marketplace again
            # (from `blocked/validation_errors` too while a pre-check ERROR is open)
            from app.services.remote_preview import request_preview

            recheck = await uow.resolution.has_open_preview_error(listing.id, version.id)
            await request_preview(uow, listing, version.id, recheck=recheck)
        return summary_of([same])
    # only the current version's issues are the listing's inbox (a past version's run is history)
    run = await insert_run(uow, listing, version.id, check, cause="revalidate" if current else None)
    if adapter is not None:
        await _sync_schema(uow, listing, version.id, adapter)
    if current:
        listing = await _revalidate(uow, principal, listing, check, adapter, on_reject)
        # SI4 A13: the questions of the current version's check (never of a past version's)
        await ask_after_check(
            uow,
            listing,
            UserActor(principal.user_id),
            canonical.images,
            template=check.template,
        )
    return summary_of([run])


def _flags(report: ErrorReport) -> set[tuple[str, str, str]]:
    return {
        (item.field, item.severity.value, item.message)
        for item in report.errors
        if item.error_code is ErrorCode.FACT_MISMATCH and item.rule_id is None
    }


def same_flags(run: Any, report: ErrorReport) -> bool:
    """SI9 B5: the rejected photo values are the one Rules input that is not a pin (CP-D12), so
    equal pins hold the same verdict only while the "facts changed" items are the same; after a
    reject the run is written again (the replay rule then holds for that run)."""
    stored = ErrorReport.model_validate(run.error_report_json)
    return _flags(stored) == _flags(report)


async def schema_changed(uow: TenantUnitOfWork, listing: Any, adapter: ErrorReport) -> bool:
    """CP-D4: the check's blocking items differ from the open `schema` error rows."""
    wanted = {dedupe_key("schema", item) for item in adapter.blocking}
    held = {row.dedupe_key for row in await uow.listing_issues.open_errors(listing.id, ["schema"])}
    return wanted != held


async def _sync_schema(
    uow: TenantUnitOfWork, listing: Any, version_id: uuid.UUID, adapter: ErrorReport
) -> None:
    await sync_issues(
        uow,
        listing,
        "schema",
        adapter,
        run_id=None,
        cause="revalidate",
        version_id=version_id,
        critical_keys=await uow.product_facts.critical_keys(),
    )


async def _revalidate(
    uow: TenantUnitOfWork,
    principal: Principal,
    listing: Any,
    check: Any,
    adapter: ErrorReport | None,
    on_reject: bool = False,
) -> Any:
    """The current version's `revalidate` move, when the outcome's status differs."""
    to, reason = human_move(check.report, adapter)
    target = _target(listing, to)
    if on_reject and listing.status == "approved" and target != "blocked":
        target = None  # warnings only: the approval stands
    if target is not None:
        listing = await uow.listings.move(
            listing,
            to=target,
            event="revalidate",
            reason=reason,
            actor=UserActor(principal.user_id),
            clear_approval=listing.status == "approved",
        )
        log.info("listing.revalidated", listing_id=str(listing.id), status=target)
    return listing


async def validate(
    principal: Principal, listing_id: uuid.UUID, version_id: uuid.UUID | None
) -> ValidationSummary:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await validate_listing(uow, principal, listing_id, version_id)
