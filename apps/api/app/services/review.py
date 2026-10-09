"""Approve and reject, with maker-checker in the API and the database (S19 T8-T12; invariants
1, 2, 3, 10; DATABASE.md §7.1, :791-829; CANON.md:152 D13; T15, T40, T42, T88).

`approve_batch(uow, principal, items, http_key=)` runs in the caller's one transaction (the
`idempotent` one, so a replay returns the stored body and nothing runs twice):

1. one `batches` row (kind `approve` if any item approves, else `reject`; `item_count` = the
   request size, which the bulk backstop reads);
2. every target listing locked in one `ORDER BY id FOR UPDATE` statement, their current versions
   read, and the stored image reports of every approved version read in one SELECT (S17b);
3. each item in its own SAVEPOINT, so a refused item rolls back alone and a retry touches only
   the failed items. An approve, in order: missing → 404; paused/archived → 409; generating or
   publishing → 409 `LISTING_BUSY`; not the current version → 409 `STALE_VERSION`; status not
   `validated|needs_review`, or the version already approved once (CP-D4) → 409
   `INVALID_STATUS_TRANSITION`; a multi-item batch and not `validated` → 409
   `BULK_APPROVE_REQUIRES_VALIDATED`; a blocking image finding → 422 `IMAGE_NON_COMPLIANT`
   (S17b's gate: an unchecked image blocks, vision only warns); no passed `rules_engine` run
   with the currently resolved pins → 409 `STALE_VERSION` (G9); the approver in the contributor
   chain, or not an active reviewer or owner (read `FOR SHARE`) → audit
   `listing.approve_denied`, item 403 `MAKER_CHECKER_VIOLATION`. Then the version's approval
   (`lv_guard` checks it again: MKC01 → the savepoint rolls back, the denial is audited, 403),
   the `approve` decision with the run's `facts_digest`, the move to `approved`, and the audit
   `listing.approved`.
   A reject checks the same up to the status (no pins, no maker-checker: a maker may reject),
   then writes the `reject` decision and moves to `blocked/rejected`; with `regenerate` it first
   passes the budget pre-check (429), enqueues a `listing_generate` job naming this request's
   key, writes the decision naming the job, and moves to `generating` (event
   `reject_regenerate`, the approval voided). The reviewer becomes the maker of the regenerated
   version.

The approver is always `principal.user_id`; nothing in the request names one. Feedback and
reasons are never logged. No adapter is imported and no publish job is written (invariant 1).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.images import ImageReport

from app.core.errors import ConflictError, DomainError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.core.request_context import current_request_id
from app.repositories.listings import UserActor
from app.repositories.uow import TenantUnitOfWork, repositories_on
from app.services.budget import BudgetExceededError, precheck
from app.services.generation import enqueue_generate
from app.services.generation_context import previous_listing
from app.services.image_gate import evaluate_image_gate
from app.services.listings import current_pins
from app.services.maker_checker import VersionLink, approver_refusal, contributor_chain

log = get_logger(__name__)

REVIEWABLE: Final = frozenset({"validated", "needs_review"})
BUSY: Final = frozenset({"generating", "publishing"})
APPROVER_ROLES: Final = frozenset({"reviewer", "owner"})
REJECTED: Final = "rejected"
# (from, event, to, reason): the transitions S19 makes, each a `listing_status_transitions` row
REVIEW_EDGES: Final = (
    ("validated", "approve", "approved", None),
    ("needs_review", "approve", "approved", None),
    ("validated", "reject", "blocked", REJECTED),
    ("needs_review", "reject", "blocked", REJECTED),
    ("validated", "reject_regenerate", "generating", None),
    ("needs_review", "reject_regenerate", "generating", None),
)
_MESSAGES: Final[Mapping[ErrorCode, str]] = {
    ErrorCode.NOT_FOUND: "listing_id not found.",
    ErrorCode.LISTING_PAUSED: "The listing is paused.",
    ErrorCode.LISTING_ARCHIVED: "The listing is archived.",
    ErrorCode.LISTING_BUSY: "The listing is generating or publishing.",
    ErrorCode.STALE_VERSION: "The version is not current, or its checks are out of date.",
    ErrorCode.INVALID_STATUS_TRANSITION: "The listing cannot be decided in its current status.",
    ErrorCode.BULK_APPROVE_REQUIRES_VALIDATED: (
        "A multi-item approve accepts only validated listings."
    ),
    ErrorCode.MAKER_CHECKER_VIOLATION: (
        "Whoever generated or edited this version cannot approve it."
    ),
    ErrorCode.BUDGET_EXCEEDED: "The daily generation budget is spent.",
    ErrorCode.IMAGE_NON_COMPLIANT: "An image blocks the approval.",
    ErrorCode.INTERNAL_ERROR: "Internal error.",
}
_STATUS: Final[Mapping[ErrorCode, int]] = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.MAKER_CHECKER_VIOLATION: 403,
    ErrorCode.IMAGE_NON_COMPLIANT: 422,
    ErrorCode.BUDGET_EXCEEDED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
}
ALREADY_APPROVED: Final = "edit or regenerate to approve again"


@dataclass(frozen=True, slots=True)
class ReviewItem:
    listing_id: uuid.UUID
    version_id: uuid.UUID
    decision: str
    reason_code: str | None = None
    feedback: str | None = None
    regenerate: bool = False


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    listing_id: uuid.UUID
    http_status: int
    listing_status: str | None = None
    job_id: uuid.UUID | None = None
    error: ErrorReport | None = None


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    batch_id: uuid.UUID
    items: list[ItemOutcome]


class Refused(Exception):  # noqa: N818 - a per-item answer, not a fault
    """One item's refusal: its status and `ErrorReport`. Raised inside the item's SAVEPOINT, so
    whatever the item wrote rolls back."""

    def __init__(self, code: ErrorCode, report: ErrorReport | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.status = _STATUS.get(code, 409)
        self.report = report or item_report(code)


def item_report(code: ErrorCode) -> ErrorReport:
    retryable = ERROR_CODE_META[code].retry_class is RetryClass.AUTO_RETRY
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=retryable,
        errors=[
            ErrorItem(
                field="listing",
                error_code=code,
                message=_MESSAGES.get(code, "The item was refused."),
                source=ErrorSource.SYSTEM,
                retryable=retryable,
            )
        ],
        request_id=current_request_id(),
    )


@dataclass(frozen=True, slots=True)
class _Ctx:
    uow: TenantUnitOfWork
    principal: Principal
    http_key: str
    batch_id: uuid.UUID
    multi: bool
    versions: Mapping[uuid.UUID, Any]
    image_reports: Mapping[uuid.UUID, Mapping[Any, ImageReport]]


def _gate(listing: Any | None, item: ReviewItem) -> Any:
    """The checks an approve and a reject share, in the documented order."""
    if listing is None:
        raise Refused(ErrorCode.NOT_FOUND)
    if listing.paused_at is not None:
        raise Refused(ErrorCode.LISTING_PAUSED)
    if listing.archived_at is not None:
        raise Refused(ErrorCode.LISTING_ARCHIVED)
    if listing.status in BUSY:
        raise Refused(ErrorCode.LISTING_BUSY)
    if listing.current_version_id is None or item.version_id != listing.current_version_id:
        raise Refused(ErrorCode.STALE_VERSION)
    if listing.status not in REVIEWABLE:
        raise Refused(ErrorCode.INVALID_STATUS_TRANSITION)
    return listing


def _version_images(listing: Any, version: Any) -> list[uuid.UUID]:
    canonical = previous_listing(listing, version)
    return [] if canonical is None else [ref.image_id for ref in canonical.images]


def _image_gate(ctx: _Ctx, listing: Any, version: Any) -> None:
    canonical = previous_listing(listing, version)
    if canonical is None or not canonical.images:
        return
    market = canonical.marketplace
    stored = {
        image_id: reports[market]
        for image_id, reports in ctx.image_reports.items()
        if market in reports
    }
    pipeline = [ImageReport.model_validate(r) for r in (version.image_reports_json or [])]
    result = evaluate_image_gate(canonical, stored, pipeline)
    if result.blocking:
        report = ErrorReport(
            status="failed",
            error_code=ErrorCode.IMAGE_NON_COMPLIANT,
            retryable=False,
            errors=[*result.blocking, *result.warnings],
            request_id=current_request_id(),
            listing_id=listing.id,
        )
        raise Refused(ErrorCode.IMAGE_NON_COMPLIANT, report)


async def _pins_digest(ctx: _Ctx, listing: Any, version: Any) -> bytes:
    """The passed run's `facts_digest` when a run with the current pins passed, else 409."""
    pins = await current_pins(ctx.uow, listing)
    run = await ctx.uow.validation_runs.find_equal_pins(
        version.id, pins.rules_version, pins.template_id, pins.template_version, pins.facts_digest
    )
    if run is None or not run.passed or run.facts_digest is None:
        raise Refused(ErrorCode.STALE_VERSION)
    digest: bytes = run.facts_digest
    return digest


async def _chain(ctx: _Ctx, listing_id: uuid.UUID, version_id: uuid.UUID) -> frozenset[uuid.UUID]:
    links = {
        row.id: VersionLink(
            id=row.id,
            origin=row.origin,
            parent_version_id=row.parent_version_id,
            changed_paths=row.changed_paths,
            generated_by_user_id=row.generated_by_user_id,
            edited_by_user_id=row.edited_by_user_id,
        )
        for row in await ctx.uow.listing_versions.chain_links(listing_id)
    }
    return contributor_chain(version_id, links)


async def _maker_checker(ctx: _Ctx, listing: Any, version: Any) -> bool:
    """True when the approver may approve: outside the chain, an active reviewer or owner."""
    approver = ctx.principal.user_id
    if approver_refusal(approver, await _chain(ctx, listing.id, version.id)) is not None:
        return False
    member = await ctx.uow.members.role_for_share(approver)
    return member is not None and member[1] and member[0] in APPROVER_ROLES


async def _audit(ctx: _Ctx, listing: Any, action: str, **kw: Any) -> None:
    await ctx.uow.audit.write(
        tenant_id=ctx.principal.tenant_id,
        actor_type="user",
        actor_user_id=ctx.principal.user_id,
        action=action,
        entity_type="listing",
        entity_id=listing.id,
        **kw,
    )


async def _deny(ctx: _Ctx, listing_id: uuid.UUID, status: str) -> ItemOutcome:
    await ctx.uow.audit.write(
        tenant_id=ctx.principal.tenant_id,
        actor_type="user",
        actor_user_id=ctx.principal.user_id,
        action="listing.approve_denied",
        entity_type="listing",
        entity_id=listing_id,
        reason=ErrorCode.MAKER_CHECKER_VIOLATION.value,
    )
    log.info("listing.approve_denied", listing_id=str(listing_id))
    refused = Refused(ErrorCode.MAKER_CHECKER_VIOLATION)
    return ItemOutcome(listing_id, refused.status, status, error=refused.report)


async def _approve_one(ctx: _Ctx, listing: Any, item: ReviewItem) -> ItemOutcome:
    _gate(listing, item)
    version = ctx.versions.get(item.version_id)
    if version is None:  # the listing's own current version: unreachable unless deleted
        raise Refused(ErrorCode.STALE_VERSION)
    if version.approved_by_user_id is not None:
        raise Refused(ErrorCode.INVALID_STATUS_TRANSITION)  # CP-D4: ALREADY_APPROVED
    if ctx.multi and listing.status != "validated":
        raise Refused(ErrorCode.BULK_APPROVE_REQUIRES_VALIDATED)
    _image_gate(ctx, listing, version)
    digest = await _pins_digest(ctx, listing, version)
    if not await _maker_checker(ctx, listing, version):
        return await _deny(ctx, listing.id, listing.status)
    uow = ctx.uow
    if not await uow.listing_versions.approve(listing.id, version.id, ctx.principal.user_id):
        raise Refused(ErrorCode.INVALID_STATUS_TRANSITION)
    await uow.review_decisions.insert_approve(
        listing.id,
        version.id,
        batch_id=ctx.batch_id,
        facts_digest=digest,
        request_id=current_request_id(),
    )
    from_status = listing.status
    moved = await uow.listings.move(
        listing,
        to="approved",
        event="approve",
        actor=UserActor(ctx.principal.user_id),
        approved_version_id=version.id,
    )
    await _audit(
        ctx,
        listing,
        "listing.approved",
        before=json.dumps({"status": from_status}),
        after=json.dumps({"status": "approved"}),
    )
    log.info("listing.approved", listing_id=str(listing.id), version_id=str(version.id))
    return ItemOutcome(listing.id, 200, moved.status)


async def _reject_one(ctx: _Ctx, listing: Any, item: ReviewItem) -> ItemOutcome:
    _gate(listing, item)
    if item.reason_code is None or item.feedback is None:  # the request model refuses it first
        raise Refused(ErrorCode.INVALID_STATUS_TRANSITION)
    uow = ctx.uow
    actor = UserActor(ctx.principal.user_id)
    from_status = listing.status
    job_id: uuid.UUID | None = None
    if item.regenerate:
        try:
            await precheck(uow, datetime.now(UTC))
        except BudgetExceededError:
            raise Refused(ErrorCode.BUDGET_EXCEEDED) from None
        job_id = await enqueue_generate(uow, ctx.principal, ctx.http_key, listing.id, None)
    await uow.review_decisions.insert_reject(
        listing.id,
        item.version_id,
        reason_code=item.reason_code,
        feedback=item.feedback,
        batch_id=ctx.batch_id,
        regeneration_job_id=job_id,
        request_id=current_request_id(),
    )
    if job_id is None:
        moved = await uow.listings.move(
            listing, to="blocked", event="reject", reason=REJECTED, actor=actor
        )
    else:
        moved = await uow.listings.move(
            listing,
            to="generating",
            event="reject_regenerate",
            actor=actor,
            clear_approval=True,
        )
    await _audit(
        ctx,
        listing,
        "listing.rejected",
        before=json.dumps({"status": from_status}),
        after=json.dumps({"status": moved.status, "status_reason": moved.status_reason}),
        reason=item.reason_code,
    )
    log.info("listing.rejected", listing_id=str(listing.id), regenerate=item.regenerate)
    return ItemOutcome(listing.id, 200, moved.status, job_id=job_id)


def _code_of(exc: DomainError) -> ErrorCode:
    if isinstance(exc, NotFoundError):
        return ErrorCode.NOT_FOUND
    if isinstance(exc, ConflictError):
        return exc.code
    return ErrorCode.INTERNAL_ERROR


async def _one(ctx: _Ctx, listings: Mapping[uuid.UUID, Any], item: ReviewItem) -> ItemOutcome:
    listing = listings.get(item.listing_id)
    # read before the savepoint: after a rollback the row's attributes are not touched again
    status: str | None = None if listing is None else listing.status
    try:
        async with ctx.uow.session.begin_nested():
            if item.decision == "approve":
                return await _approve_one(ctx, listing, item)
            return await _reject_one(ctx, listing, item)
    except Refused as refused:
        return ItemOutcome(item.listing_id, refused.status, status, error=refused.report)
    except DomainError as exc:
        # a guard's refusal (the savepoint is rolled back): MKC01 is the maker-checker backstop
        code = _code_of(exc)
        if code is ErrorCode.MAKER_CHECKER_VIOLATION and status is not None:
            async with ctx.uow.session.begin_nested():
                return await _deny(ctx, item.listing_id, status)
        if code is ErrorCode.INTERNAL_ERROR:
            raise
        mapped = Refused(code)
        return ItemOutcome(item.listing_id, mapped.status, status, error=mapped.report)


async def approve_batch(
    uow: TenantUnitOfWork,
    principal: Principal,
    items: Sequence[ReviewItem],
    *,
    http_key: str,
) -> BatchOutcome:
    if not items:
        raise ValueError("an approve batch has at least one item")
    kind = "approve" if any(i.decision == "approve" for i in items) else "reject"
    batch = await uow.batches.insert(
        kind, len(items), principal.user_id, request_id=current_request_id()
    )
    listings = await uow.listings.lock_many([i.listing_id for i in items])
    current = [lst.current_version_id for lst in listings.values() if lst.current_version_id]
    versions = await uow.listing_versions.get_many(current)
    image_ids = [
        image_id
        for i in items
        if i.decision == "approve"
        and (lst := listings.get(i.listing_id)) is not None
        and (v := versions.get(i.version_id)) is not None
        and v.listing_id == lst.id
        for image_id in _version_images(lst, v)
    ]
    reports = await uow.images.live_reports_for_images(image_ids) if image_ids else {}
    ctx = _Ctx(
        uow=uow,
        principal=principal,
        http_key=http_key,
        batch_id=batch.id,
        multi=len(items) > 1,
        versions=versions,
        image_reports=reports,
    )
    return BatchOutcome(batch.id, [await _one(ctx, listings, item) for item in items])


def outcome_body(outcome: BatchOutcome) -> dict[str, Any]:
    """The `BatchResult` body (JSON-ready; the router validates it)."""
    return {
        "batch_id": str(outcome.batch_id),
        "items": [
            {
                "listing_id": str(i.listing_id),
                "http_status": i.http_status,
                "listing_status": i.listing_status,
                "job_id": None if i.job_id is None else str(i.job_id),
                "error": None if i.error is None else i.error.model_dump(mode="json"),
            }
            for i in outcome.items
        ],
    }


async def accept_review(
    principal: Principal, key: str, path: str, payload: bytes, items: Sequence[ReviewItem]
) -> dict[str, Any]:
    """The route's idempotent transaction: 200 with per-item results, stored under the key."""
    from app.services.idempotency import idempotent

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        return 200, outcome_body(await approve_batch(uow, principal, items, http_key=key))

    _status, stored = await idempotent(principal, key, "POST", path, payload, handler)
    return stored
