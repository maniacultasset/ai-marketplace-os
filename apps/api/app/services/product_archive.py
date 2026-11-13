"""Archive a product (`POST /products/{product_id}/archive`, `.../unarchive`): put it away without
deleting it (client Chrome pass 2, 12 Nov 2026: the product menu offered only Delete).

`archive_product(principal, product_id)`, one transaction, in the delete's lock order:

1. the same refusals as `DELETE /products/{product_id}` (`product_delete.check_unseen`, `cancel_product_jobs`),
   so nothing a marketplace has seen is archived here and no running work is cut: missing or
   another tenant's → 404; a variant → 422 at `product_id`; a listing the marketplace has seen →
   409 `INVALID_STATUS_TRANSITION`; one being sent → 409 `SUBMISSION_IN_FLIGHT`; one generating
   → 409 `LISTING_BUSY`. No marketplace is called;
2. its queued jobs are cancelled (no AI is spent on it) and its live questions are `cancelled`
   with reason `listing_archived`;
3. its listings are archived (DATABASE.md "Pause and archive": generate, publish, review, the
   sync and the lists all skip them) and the style and its live variants get `archived_at`;
4. one `product.archived` audit row and one `listing.archived` row per listing.

Unlike delete, photos, facts, variants and the history stay. The product leaves `GET /products`
(`?archived=true` lists the archived ones) and generate refuses it (409 `LISTING_ARCHIVED`).

`unarchive_product` clears `archived_at` and lifts the archive on its listings (event
`unarchive`), audited as `product.unarchived`. The cancelled questions stay closed: a regenerate
asks again what it still needs. Both calls answer the product; archiving an archived product (or
unarchiving a live one) changes nothing and answers it again.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from mkos_schemas.codes import ErrorCode

from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services import product_delete
from app.services.products import ProductView, full_view

log = get_logger(__name__)

ARCHIVED_TASK_REASON = "listing_archived"


async def archive_product(principal: Principal, product_id: uuid.UUID) -> ProductView:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.products.get_for_update(product_id)
        if row is not None and row.parent_product_id is None and row.archived_at is not None:
            return await full_view(uow, row)  # already archived: nothing changes
        plan = await product_delete.check_unseen(uow, product_id)
        listing_ids = [listing.id for listing in plan.listings]
        job_ids = await product_delete.cancel_product_jobs(uow, plan, listing_ids)
        for listing in (await uow.listings.lock_many(listing_ids)).values() if listing_ids else ():
            if listing.status in ("generating", *product_delete.IN_FLIGHT):
                raise ConflictError(ErrorCode.LISTING_BUSY, "listings")
        tasks = await uow.human_tasks.cancel_for_products(plan.family, ARCHIVED_TASK_REASON)
        archived = await uow.listings.archive_for_deleted_product(listing_ids, principal.user_id)
        await uow.products.set_archived(plan.family, archived=True)
        await _audit(uow, principal, plan.family[0], "product.archived", archived, tasks, job_ids)
        log.info(
            "product.archived",
            product_id=str(product_id),
            listings=len(archived),
            tasks=len(tasks),
            jobs=len(job_ids),
        )
        return await _view(uow, product_id)


async def unarchive_product(principal: Principal, product_id: uuid.UUID) -> ProductView:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.products.get_for_update(product_id)
        if row is None:
            raise NotFoundError("product_id")
        if row.parent_product_id is not None:  # a size goes with its style, as for delete
            raise InputError(ErrorCode.VALIDATION_FAILED, "product_id")
        if row.archived_at is None:
            return await full_view(uow, row)  # not archived: nothing changes
        family = [row.id, *await uow.products.live_child_ids(row.id)]
        await uow.products.set_archived(family, archived=False)
        lifted: list[uuid.UUID] = []
        for family_id in family:
            lifted.extend(await uow.listings.unarchive_for_revived_product(family_id))
        await _audit(uow, principal, row.id, "product.unarchived", lifted, (), ())
        log.info("product.unarchived", product_id=str(product_id), listings=len(lifted))
        return await _view(uow, product_id)


async def _view(uow: TenantUnitOfWork, product_id: uuid.UUID) -> ProductView:
    # re-read (populate_existing): the core UPDATE left the session's copy stale
    row = await uow.products.get_for_update(product_id)
    if row is None:  # pragma: no cover - the row is locked by this transaction
        raise NotFoundError("product_id")
    return await full_view(uow, row)


async def _audit(
    uow: TenantUnitOfWork,
    principal: Principal,
    product_id: uuid.UUID,
    action: str,
    listings: Sequence[uuid.UUID],
    tasks: Sequence[uuid.UUID],
    jobs: Sequence[uuid.UUID],
) -> None:
    archiving = action == "product.archived"
    reason = (
        f"{action.split('.')[1]};tasks:{len(tasks)};jobs:{len(jobs)};"
        f"listings:{','.join(str(i) for i in listings)}"
    )[:1000]
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action=action,
        entity_type="product",
        entity_id=product_id,
        reason=reason,
    )
    at = datetime.now(UTC).isoformat()
    for listing_id in listings:
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="listing.archived" if archiving else "listing.unarchived",
            entity_type="listing",
            entity_id=listing_id,
            before=json.dumps({"archived_at": None if archiving else at}),
            after=json.dumps({"archived_at": at if archiving else None}),
            reason="product_archived" if archiving else "product_unarchived",
        )


__all__ = ["ARCHIVED_TASK_REASON", "archive_product", "unarchive_product"]
