"""`POST /products/{product_id}/recategorize` (SI9 add-on T15): the category fix for a product that
already has listings (`PATCH` refuses a category change once a listing exists).

`recategorize_product(principal, product_id, category)`, in one transaction under the product's
row lock, then its listings' locks in id order:

1. the product: missing or another tenant's → 404; a variant follows its style → 422
   `VALIDATION_FAILED` at `category`; the same category → 200, nothing written (idempotent);
   a style with live variants → 409 `INVALID_STATUS_TRANSITION` (the database keeps a style's
   category while it has variants, `variant_guard`);
2. every listing: one the marketplace has seen (published, publishing, ever published, or with
   any submission) → 409 `INVALID_STATUS_TRANSITION`, nothing changed (a published listing is
   never touched); a generating one → 409 `LISTING_BUSY`;
3. the category must be mapped on each of the product's marketplaces (a current template, with an
   Amazon product type) → else 422 `VALIDATION_FAILED` at `category`;
4. the product's category is written (a seller fact, as `PATCH`); each listing takes the new
   category, so its next generation uses that category's template. Its content was written for
   the old category, so it is superseded: a `validated`, `needs_review` or `approved` listing
   moves to `blocked/validation_errors` (event `revalidate`, an approval voided) until it is
   regenerated; any other status is kept (none of them can be approved or published);
5. one `product.recategorized` audit row (the category before and after under `provenance`, the
   superseded listing ids in `reason`).

SI13 T8 (P:A2, D9; CP-D7, CP-D19): a seen listing passes step 2 only when it is `failed`, was
never live (`first_published_at` NULL) and has an open marketplace `CATEGORY_MISMATCH` row on its
current version (Amazon said the category is wrong). It then moves `failed ->
blocked/validation_errors` on the existing user `edit` edge (the approval voided), and the audit
reason starts `recategorize;issue:<id>;`. The issue stays open until the regenerate makes a new
version. A published, publishing, ever-live or `blocked` listing, and any seen listing without
that row, still answers 409.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.listings import UserActor
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.product_facts import seller_column_fact
from app.services.products import ProductView, full_view

log = get_logger(__name__)

SEEN: Final = frozenset({"publishing", "published"})
SUPERSEDED: Final = frozenset({"validated", "needs_review", "approved"})


async def recategorizable(uow: TenantUnitOfWork, listing: Any) -> bool:
    """SI13 (CP-D7): whether the seller may recategorise this listing's product, read only. Never
    for a listing that was ever live, a style with live variants (the DB keeps a family's
    category) or a product with another listing the marketplace has seen. Then either the
    listing is `failed` (the T8 widening after a rejection), or the marketplace has never seen it
    and it is not generating (today's route already allows that). The planner's `recategorize`
    seller step and the service use the same rule."""
    if listing.first_published_at is not None:
        return False
    if listing.status != "failed" and (
        listing.status in SEEN
        or listing.status == "generating"
        or await uow.submissions.latest(listing.id) is not None
    ):
        return False
    product = await uow.products.get_by_id(listing.product_id)
    if product is None or product.parent_product_id is not None:
        return False
    if await uow.products.live_child_ids(product.id):
        return False
    for other in await uow.listings.for_product(product.id):
        if other.id == listing.id:
            continue
        if (
            other.status in SEEN
            or other.first_published_at is not None
            or await uow.submissions.latest(other.id) is not None
        ):
            return False
    return True


async def _rejected_category(uow: TenantUnitOfWork, listing: Any) -> uuid.UUID | None:
    """SI13 T8: the open category issue that lets a rejected listing be recategorised: `failed`,
    never live, and Amazon's CATEGORY_MISMATCH open on the current version."""
    if listing.status != "failed" or listing.first_published_at is not None:
        return None
    found: uuid.UUID | None = await uow.listing_issues.open_adapter_category_issue(
        listing.id, listing.current_version_id
    )
    return found


def _audit_reason(listings: Any, rejected: dict[uuid.UUID, uuid.UUID]) -> str:
    ids = ",".join(sorted(str(x.id) for x in listings))
    if not rejected:
        return ("listings:" + ids)[:500]
    issue = sorted(str(i) for i in rejected.values())[0]
    return f"recategorize;issue:{issue};listings:{ids}"[:500]


async def recategorize_product(
    principal: Principal, product_id: uuid.UUID, category: str
) -> ProductView:
    now = datetime.now(UTC)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.products.get_for_update(product_id)
        if row is None:
            raise NotFoundError("product_id")
        if row.parent_product_id is not None:
            raise InputError(ErrorCode.VALIDATION_FAILED, "category")
        if row.category == category:
            return await full_view(uow, row)
        if await uow.products.live_child_ids(row.id):
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "product_variants")
        listings = await uow.listings.for_product_for_update(row.id)
        rejected: dict[uuid.UUID, uuid.UUID] = {}  # SI13: listing -> its open category issue
        for listing in listings:
            seen = (
                listing.status in SEEN
                or listing.first_published_at is not None
                or await uow.submissions.latest(listing.id) is not None
            )
            if seen:
                issue = await _rejected_category(uow, listing)
                if issue is None:
                    raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listing_published")
                rejected[listing.id] = issue
            if listing.status == "generating":
                raise ConflictError(ErrorCode.LISTING_BUSY, "listings")
        markets = sorted({listing.marketplace for listing in listings}) or ["amazon"]
        for marketplace in markets:
            if not await uow.templates.is_mapped(marketplace, category):
                raise InputError(ErrorCode.VALIDATION_FAILED, "category")
        before = row.category
        column_fact = seller_column_fact(now=now)
        await uow.facts.set_typed(row.id, "category", category, column_fact)
        actor = UserActor(principal.user_id)
        for listing in listings:
            await uow.listings.set_category(listing.id, category)
            if listing.id in rejected:
                # CP-D19: the existing user `edit` edge (failed -> blocked, voids the approval)
                await uow.listings.move(
                    listing,
                    to="blocked",
                    event="edit",
                    reason="validation_errors",
                    actor=actor,
                    clear_approval=True,
                )
            elif listing.status in SUPERSEDED and listing.paused_at is None:
                await uow.listings.move(
                    listing,
                    to="blocked",
                    event="revalidate",
                    reason="validation_errors",
                    actor=actor,
                    clear_approval=listing.status == "approved",
                )
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="product.recategorized",
            entity_type="product",
            entity_id=row.id,
            # the product audit keeps `provenance` only: the category envelopes, before and after
            before=json.dumps({"provenance": {"category": {"value": before}}}),
            after=json.dumps({"provenance": {"category": {**column_fact, "value": category}}}),
            reason=_audit_reason(listings, rejected),
        )
        log.info("product.recategorized", product_id=str(row.id), listings=len(listings))
        await uow.session.refresh(row)
        return await full_view(uow, row)
