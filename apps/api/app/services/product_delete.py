"""Delete a product (`DELETE /products/{product_id}`, `POST /products/bulk-delete`): a soft
delete that stops every piece of work on the product and keeps the history.

`delete_one(uow, principal, product_id)`, in the caller's transaction, under the product's row
lock and then its listings' locks (NOWAIT, the recategorize order: a generation persist takes the
listing before the product):

1. the product: missing, already deleted or another tenant's → 404 `NOT_FOUND`; a variant →
   422 `VALIDATION_FAILED` at `product_id` (a size goes with its style);
2. every listing of the product and its live variants is checked first, so a refusal changes
   nothing. One the marketplace has seen (`published`, ever published, any submission, or a
   marketplace state other than `not_submitted`) → 409 `INVALID_STATUS_TRANSITION`; `publishing`
   or a live `listing_publish` job → 409 `SUBMISSION_IN_FLIGHT`; a running job on a listing (a
   generation or a dry run) or a listing another transaction holds → 409 `LISTING_BUSY`. No
   marketplace adapter is ever called;
3. the product's queued jobs (its listings' jobs, its photos' checks and size-chart reads, its
   image candidates, its classification) are cancelled with their queue entries and terminal
   hooks, as `POST /jobs/{id}/cancel` does, so no queued AI call is spent. A running photo,
   candidate or classification job finishes against rows that are gone and stores nothing;
4. its live tasks are `cancelled` with reason `product_deleted`; its open issues stay open on
   their archived listing and leave the inbox (the issue resolution vocabulary is closed in
   the 2.1.0 contract, so no resolution is invented for them);
5. its listings are archived (DATABASE.md "Pause and archive": the generate and publish gates,
   the sync scheduler, review and resolution all skip an archived listing);
6. photos: its live images are soft-deleted and its open candidates retired. No object is
   deleted here: the daily `sweep_deleted_image_objects` removes the derived and public copies,
   the 30-day candidate sweep removes candidate objects, and the private original stays until
   the tenant purge (the images retention rule: "a replaced image is soft-deleted and its object
   kept");
7. a style's available matrix cells become `unavailable/discontinued`, then its variants and the
   style get `deleted_at`;
8. one `product.deleted` audit row (the counts and the archived listing ids under `reason`) and
   one `listing.archived` row per archived listing.

A CSV import of the same SKU later revives the row (`upsert_by_sku`) and lifts the archive on
its listings (event `unarchive`, a `listing.unarchived` audit row), so it can be drafted again;
its photos and closed tasks stay gone. The app has no other undo.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.errors import ConflictError, DomainError, InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.core.request_context import current_request_id
from app.repositories.jobs import cancel_queue_entry
from app.repositories.transactions import RowDbError, row_savepoint
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow
from app.repositories.variants import MatrixCellRepository
from app.services.job_hooks import run_terminal_hook

log = get_logger(__name__)

SEEN: Final = frozenset({"published"})
IN_FLIGHT: Final = frozenset({"publishing"})
UNSEEN_MARKET_STATE: Final = "not_submitted"


@dataclass(frozen=True, slots=True)
class Deleted:
    product_id: uuid.UUID
    product_ids: list[uuid.UUID]
    listing_ids: list[uuid.UUID]
    task_ids: list[uuid.UUID]
    issue_ids: list[uuid.UUID]
    job_ids: list[uuid.UUID]
    image_ids: list[uuid.UUID]


@dataclass(frozen=True, slots=True)
class ItemResult:
    product_id: uuid.UUID
    http_status: int
    error: ErrorReport | None = None


@dataclass(slots=True)
class _Plan:
    family: list[uuid.UUID]
    listings: list[Any] = field(default_factory=list)


async def delete_product(principal: Principal, product_id: uuid.UUID) -> Deleted:
    """`DELETE /products/{product_id}`: one transaction; a second call is 404."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await delete_one(uow, principal, product_id)


async def delete_one(uow: TenantUnitOfWork, principal: Principal, product_id: uuid.UUID) -> Deleted:
    plan = await _check(uow, product_id)
    listing_ids = [listing.id for listing in plan.listings]
    job_ids = await _cancel_jobs(uow, plan, listing_ids)
    # a cancelled generation's hook moved its listing out of `generating`; nothing else may be
    for listing in (await uow.listings.lock_many(listing_ids)).values() if listing_ids else ():
        if listing.status in ("generating", *IN_FLIGHT):
            raise ConflictError(ErrorCode.LISTING_BUSY, "listings")
    tasks = await uow.human_tasks.cancel_for_products(plan.family)
    issues = await uow.listing_issues.open_ids_for_listings(listing_ids)
    archived = await uow.listings.archive_for_deleted_product(listing_ids, principal.user_id)
    for family_id in plan.family:
        await uow.images.lock_product_images(family_id)
    images = await uow.images.soft_delete_for_products(plan.family)
    await uow.image_candidates.retire_for_products(plan.family)
    style, children = plan.family[0], plan.family[1:]
    if children:
        await MatrixCellRepository(uow.session, principal.tenant_id).discontinue_for_style(style)
    written = await uow.products.soft_delete_many([*children, style])
    if style not in written:  # a concurrent delete won the row
        raise NotFoundError("product_id")
    await _audit(uow, principal, style, archived, tasks, issues, job_ids, images, children)
    log.info(
        "product.deleted",
        product_id=str(style),
        variants=len(children),
        listings=len(archived),
        tasks=len(tasks),
        jobs=len(job_ids),
        images=len(images),
    )
    return Deleted(style, plan.family, archived, tasks, issues, job_ids, images)


async def _check(uow: TenantUnitOfWork, product_id: uuid.UUID) -> _Plan:
    row = await uow.products.get_for_update(product_id)
    if row is None:
        raise NotFoundError("product_id")
    if row.parent_product_id is not None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "product_id")
    plan = _Plan(family=[row.id, *await uow.products.live_child_ids(row.id)])
    for family_id in plan.family:
        plan.listings.extend(await uow.listings.for_product_for_update(family_id))
    for listing in plan.listings:
        if listing.status in IN_FLIGHT:
            raise ConflictError(ErrorCode.SUBMISSION_IN_FLIGHT, "listing_publishing")
        if (
            listing.status in SEEN
            or listing.first_published_at is not None
            or listing.marketplace_state != UNSEEN_MARKET_STATE
            or await uow.submissions.latest(listing.id) is not None
        ):
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listing_published")
    return plan


async def _cancel_jobs(
    uow: TenantUnitOfWork, plan: _Plan, listing_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """Cancel the product's queued jobs (refusing first while one of its listings has a running
    job or a live publish); the ids cancelled."""
    images = await uow.images.live_ids_for_products(plan.family)
    candidates = await uow.image_candidates.ids_for_products(plan.family)
    jobs = await uow.job_runs.live_for_product(
        listing_ids=listing_ids,
        image_ids=images,
        candidate_ids=candidates,
        product_ids=plan.family,
    )
    for job in jobs:
        if job.kind == JobKind.LISTING_PUBLISH.value:
            raise ConflictError(ErrorCode.SUBMISSION_IN_FLIGHT, "listing_publishing")
        if job.listing_id is not None and job.status == "running":
            raise ConflictError(ErrorCode.LISTING_BUSY, "listings")
    cancelled: list[uuid.UUID] = []
    for job in jobs:
        if job.status != "queued":
            continue  # a running photo, candidate or classification job stores nothing now
        done = await uow.job_runs.cancel_queued(job.id)
        if done is None:
            continue
        await cancel_queue_entry(uow.session, done)
        await run_terminal_hook(uow.session, done, None)
        cancelled.append(done.id)
    return cancelled


async def _audit(
    uow: TenantUnitOfWork,
    principal: Principal,
    product_id: uuid.UUID,
    archived: Sequence[uuid.UUID],
    tasks: Sequence[uuid.UUID],
    issues: Sequence[uuid.UUID],
    jobs: Sequence[uuid.UUID],
    images: Sequence[uuid.UUID],
    children: Sequence[uuid.UUID],
) -> None:
    reason = (
        f"product_deleted;variants:{len(children)};tasks:{len(tasks)};issues:{len(issues)};"
        f"jobs:{len(jobs)};images:{len(images)};listings:{','.join(str(i) for i in archived)}"
    )[:1000]
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action="product.deleted",
        entity_type="product",
        entity_id=product_id,
        reason=reason,
    )
    at = datetime.now(UTC).isoformat()
    for listing_id in archived:
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="listing.archived",
            entity_type="listing",
            entity_id=listing_id,
            before=json.dumps({"archived_at": None}),
            after=json.dumps({"archived_at": at}),
            reason="product_deleted",
        )


# ---- bulk ----

_MESSAGES: Final = {
    ErrorCode.NOT_FOUND: "product_id not found.",
    ErrorCode.VALIDATION_FAILED: "A size is deleted with its style; delete the style instead.",
    ErrorCode.INVALID_STATUS_TRANSITION: (
        "A listing of this product was sent to a marketplace, so it cannot be deleted here."
    ),
    ErrorCode.SUBMISSION_IN_FLIGHT: "A listing of this product is being sent to a marketplace.",
    ErrorCode.LISTING_BUSY: "A listing of this product is generating; try again in a minute.",
    ErrorCode.INTERNAL_ERROR: "Internal error.",
}


def _report(code: ErrorCode) -> ErrorReport:
    retryable = ERROR_CODE_META[code].retry_class is RetryClass.AUTO_RETRY
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=retryable,
        errors=[
            ErrorItem(
                field="request",
                error_code=code,
                message=_MESSAGES.get(code, "The item was refused."),
                source=ErrorSource.SYSTEM,
                retryable=retryable,
            )
        ],
        request_id=current_request_id(),
    )


def _refusal(exc: DomainError) -> tuple[int, ErrorCode]:
    if isinstance(exc, NotFoundError):
        return 404, ErrorCode.NOT_FOUND
    if isinstance(exc, InputError):
        return 422, exc.code
    if isinstance(exc, ConflictError) and exc.code is not ErrorCode.INTERNAL_ERROR:
        return 409, exc.code
    return 500, ErrorCode.INTERNAL_ERROR


async def bulk_delete(
    principal: Principal, key: str, path: str, payload: bytes, product_ids: Sequence[uuid.UUID]
) -> list[ItemResult]:
    """`POST /products/bulk-delete`: one idempotent transaction, each product in its own
    savepoint, so one refusal rolls back only that product. 200 with each item's own
    `http_status` (204 deleted, 404, 409, 422); a replay returns the stored answer."""
    from app.services.idempotency import idempotent

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        items: list[dict[str, Any]] = []
        for product_id in product_ids:
            status, error = 204, None
            try:
                async with row_savepoint(session):
                    await delete_one(uow, principal, product_id)
            except DomainError as exc:
                status, code = _refusal(exc)
                error = _report(code)
            except RowDbError:
                status, error = 409, _report(ErrorCode.LISTING_BUSY)
            items.append(
                {
                    "product_id": str(product_id),
                    "http_status": status,
                    "error": None if error is None else error.model_dump(mode="json"),
                }
            )
        return 200, {"items": items}

    _status, body = await idempotent(principal, key, "POST", path, payload, handler)
    return [
        ItemResult(
            product_id=uuid.UUID(item["product_id"]),
            http_status=int(item["http_status"]),
            error=None if item["error"] is None else ErrorReport.model_validate(item["error"]),
        )
        for item in body["items"]
    ]


check_unseen = _check
"""The delete's refusals, shared with archive (`app.services.product_archive`)."""
cancel_product_jobs = _cancel_jobs
"""The delete's job cancel (and its running-job refusals), shared with archive."""

__all__ = [
    "Deleted",
    "ItemResult",
    "bulk_delete",
    "cancel_product_jobs",
    "check_unseen",
    "delete_one",
    "delete_product",
]
