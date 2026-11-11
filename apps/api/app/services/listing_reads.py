"""Listing reads for review (S19 T5-T7; AC 12): the list (the review queue), one listing's detail
and its audit history. Every read runs in the caller's tenant unit of work, so another tenant's
listing reads as missing (404), exactly as an unknown id.

- `list_listings`: `status` `validated` or `needs_review` is the review queue, oldest first by
  `status_changed_at` (paused and archived listings excluded); any other filter is newest first by
  `last_event_at`. The two orders page with different cursor tags (`order_tag`).
- `detail`: the current version's content and its validation summary, plus the `ETag` value
  (the current version id; None while the listing has no version).
- both carry the display state (`listing_display`): a stale reviewable listing is validated again
  first (detail; every row of a product; at most `LIST_RECHECK_CAP` rows of a list's first page),
  and each row's open blocking issues are counted in one query.
- `history`: the listing's `audit_log` rows, newest first; no actor email, no IP.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from app.core.errors import NotFoundError
from app.core.rbac import Principal
from app.repositories.listings import QUEUE_STATUSES, ListingFilter
from app.repositories.uow import tenant_uow
from app.services import listing_display
from app.services.generation_context import previous_listing
from app.services.listing_display import needs_you_counts, recheck_page, recheck_stale
from app.services.listings import listing_fields, to_detail
from app.services.maker_checker import (
    APPROVER_ROLES,
    SELF_APPROVER_ROLES,
    ApproveHint,
    approve_block,
    is_self_approval,
)

QUEUE_TAG: Final = "queue"
RECENT_TAG: Final = "recent"
Keyset = tuple[datetime, uuid.UUID]


def order_tag(status: str | None) -> str:
    """The cursor tag of the order a list with this `status` filter pages in."""
    return QUEUE_TAG if status in QUEUE_STATUSES else RECENT_TAG


@dataclass(frozen=True, slots=True)
class ListingsPage:
    items: list[dict[str, Any]]
    has_more: bool
    next_key: Keyset | None
    tag: str


@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: list[dict[str, Any]]
    has_more: bool
    next_key: Keyset | None


async def list_listings(
    principal: Principal,
    *,
    status: str | None,
    marketplace: str | None,
    product_id: uuid.UUID | None,
    after: Keyset | None,
    limit: int,
) -> ListingsPage:
    filters = ListingFilter(status=status, marketplace=marketplace, product_id=product_id)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, more = await uow.listings.page(filters, after=after, limit=limit)
        # the cursor is the page as read: a re-check below moves `status_changed_at`
        next_key: Keyset | None = None
        if more and rows:
            last = rows[-1]
            next_key = (last.status_changed_at if filters.queue else last.last_event_at, last.id)
        if product_id is not None or after is None:
            cap = None if product_id is not None else listing_display.LIST_RECHECK_CAP
            rows = await recheck_page(uow, principal, rows, cap=cap)
        counts = await needs_you_counts(uow, rows)
    return ListingsPage(
        items=[listing_fields(r, counts.get(r.id, 0)) for r in rows],
        has_more=more,
        next_key=next_key,
        tag=order_tag(status),
    )


async def detail(principal: Principal, listing_id: uuid.UUID) -> tuple[dict[str, Any], str | None]:
    """The `ListingDetail` fields and the `ETag` value (`"<current_version_id>"` or None)."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        listing = await uow.listings.get(listing_id)
        if listing is None:
            raise NotFoundError("listing_id")
        listing = await recheck_stale(uow, principal, listing)
        version = await uow.listing_versions.current(listing)
        summary = None if version is None else await uow.validation_runs.summary_for(version.id)
        content = previous_listing(listing, version)
        counts = await needs_you_counts(uow, [listing])
        body = to_detail(listing, content, summary, counts.get(listing.id, 0))
        body["can_approve"], body["approve_hint"], body["self_approval"] = await _approve_hint(
            uow, principal, listing, version
        )
    return body, etag_of(body.get("current_version_id"))


async def _approve_hint(
    uow: Any, principal: Principal, listing: Any, version: Any
) -> tuple[bool | None, ApproveHint | None, bool | None]:
    """SI10 T16 (CP-D16): whether role eligibility and maker-checker let the caller approve the
    current version, why not, and (ADR-0031) whether that approval would be a self-approval.
    Null unless the listing is reviewable with a version; the approve route's other gates
    (permission, stale pins, images) still answer on the click.

    Blocked by `own_work` with nobody else able to approve: `owner_can_enable` for an owner (the
    brand's `owner_self_approval` is then off: with it on, an owner is never `own_work`), else
    `no_other_approver`."""
    from app.services.review import REVIEWABLE, version_chain

    if version is None or listing.status not in REVIEWABLE:
        return None, None, None
    chain = await version_chain(uow, listing.id, version.id)
    member = await uow.members.approver_state(principal.user_id)
    role = None if member is None else member[0]
    block = approve_block(
        principal.user_id,
        role=role,
        active=member is not None and member[1],
        chain=chain,
        owner_self_approval=await uow.tenants.owner_self_approval(),
    )
    if block is None:
        return True, None, is_self_approval(principal.user_id, chain)
    if block == "own_work" and not (await uow.members.approver_ids(APPROVER_ROLES)) - chain:
        return (
            False,
            "owner_can_enable" if role in SELF_APPROVER_ROLES else "no_other_approver",
            False,
        )
    return False, block, False


def etag_of(version_id: object) -> str | None:
    return None if version_id is None else f'"{version_id}"'


def _event(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "action": row.action,
        "actor_type": row.actor_type,
        "actor_user_id": row.actor_user_id,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "before": row.before,
        "after": row.after,
        "reason": row.reason,
        "request_id": row.request_id,
        "job_id": row.job_id,
        "occurred_at": row.occurred_at,
    }


async def history(
    principal: Principal, listing_id: uuid.UUID, *, after: Keyset | None, limit: int
) -> HistoryPage:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        if await uow.listings.get(listing_id) is None:
            raise NotFoundError("listing_id")
        rows, more = await uow.audit_log.page_for_listing(listing_id, after=after, limit=limit)
        reviews = await _reviews(uow, listing_id, rows)
    next_key = (rows[-1].occurred_at, rows[-1].id) if more and rows else None
    items = [{**_event(r), "review": reviews.get((r.action, r.request_id))} for r in rows]
    return HistoryPage(items=items, has_more=more, next_key=next_key)


REVIEW_ACTIONS: Final = {"listing.approved": "approve", "listing.rejected": "reject"}


async def _reviews(
    uow: Any, listing_id: uuid.UUID, rows: list[Any]
) -> dict[tuple[str, str | None], dict[str, Any]]:
    """Prod QA 2026-11-09: an approve or reject event names its version and reason (the
    `review_decisions` row its request wrote; the audit row's allow-list keeps only statuses).
    The reviewer's feedback never: it is untrusted free text (test_s19_review_attacks)."""
    wanted = [r.request_id for r in rows if r.action in REVIEW_ACTIONS and r.request_id]
    out: dict[tuple[str, str | None], dict[str, Any]] = {}
    for (
        request_id,
        decision,
        version_id,
        number,
        reason,
        self_approved,
    ) in await uow.review_decisions.for_requests(listing_id, wanted):
        action = next(a for a, d in REVIEW_ACTIONS.items() if d == decision)
        out[(action, request_id)] = {
            "listing_version_id": version_id,
            "version": number,
            "reason_code": reason,
            "self_approval": decision == "approve" and bool(self_approved),
        }
    return out
