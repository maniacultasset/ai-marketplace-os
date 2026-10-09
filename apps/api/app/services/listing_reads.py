"""Listing reads for review (S19 T5-T7; AC 12): the list (the review queue), one listing's detail
and its audit history. Every read runs in the caller's tenant unit of work, so another tenant's
listing reads as missing (404), exactly as an unknown id.

- `list_listings`: `status` `validated` or `needs_review` is the review queue, oldest first by
  `status_changed_at` (paused and archived listings excluded); any other filter is newest first by
  `last_event_at`. The two orders page with different cursor tags (`order_tag`).
- `detail`: the current version's content and its validation summary, plus the `ETag` value
  (the current version id; None while the listing has no version).
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
from app.services.generation_context import previous_listing
from app.services.listings import listing_fields, to_detail

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
    next_key: Keyset | None = None
    if more and rows:
        last = rows[-1]
        next_key = (last.status_changed_at if filters.queue else last.last_event_at, last.id)
    return ListingsPage(
        items=[listing_fields(r) for r in rows],
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
        version = await uow.listing_versions.current(listing)
        summary = None if version is None else await uow.validation_runs.summary_for(version.id)
        content = previous_listing(listing, version)
        body = to_detail(listing, content, summary)
    return body, etag_of(body.get("current_version_id"))


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
    next_key = (rows[-1].occurred_at, rows[-1].id) if more and rows else None
    return HistoryPage(items=[_event(r) for r in rows], has_more=more, next_key=next_key)
