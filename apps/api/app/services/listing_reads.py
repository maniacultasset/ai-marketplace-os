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
- `history`: the listing's `audit_log` rows, newest first; no actor email, no IP. A person's row
  carries `actor_name` (item 17): a member's display name, else email, read in this tenant only.
- `versions`: the listing's versions, newest first, each with who asked Kuinn for it (or imported
  it), who edited it and who approved it, named the same way, and the self-approval mark.
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
        body.update(await approve_fields(uow, principal, listing, version))
        body["affected_sections"] = await affected(uow, listing, version)
    return body, etag_of(body.get("current_version_id"))


async def approve_fields(
    uow: Any, principal: Principal, listing: Any, version: Any
) -> dict[str, Any]:
    """`can_approve`, `approve_hint` and `self_approval` of a `ListingDetail` (`_approve_hint`)."""
    can, hint, own = await _approve_hint(uow, principal, listing, version)
    return {"can_approve": can, "approve_hint": hint, "self_approval": own}


async def affected(uow: Any, listing: Any, version: Any) -> list[str]:
    """Client feedback 14: the sections whose facts the seller answered after the last AI run
    that wrote them (`sections.stale_sections`, the fully locked ones left out). A fresh
    generation rewrote every section; a regeneration the scope its model runs record (a
    deterministic fix, an edit or a fact-only version wrote none). Empty while there is no
    version to update or a run is writing one."""
    from app.services.sections import stale_sections

    if version is None or listing.status in ("created", "generating"):
        return []
    answers = await uow.human_tasks.fact_answers(listing.product_id)
    if not answers:
        return []
    scopes: dict[Any, list[list[str] | None]] = {}
    for version_id, scope in await uow.agent_runs.version_scopes(listing.id):
        scopes.setdefault(version_id, []).append(scope)
    refreshes: list[tuple[Any, list[str] | None]] = []
    for version_id, origin, at in await uow.listing_versions.ai_written(listing.id):
        runs = scopes.get(version_id)
        if origin == "generation" or (runs and None in runs):
            refreshes.append((at, None))
        elif runs:
            refreshes.append((at, sorted({p for scope in runs if scope for p in scope})))
    locked = getattr(listing, "locked_paths", None) or ()
    return list(stale_sections(answers, refreshes, locked))


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
        people = [r.actor_user_id for r in rows if r.actor_type == "user" and r.actor_user_id]
        names = await uow.members.names(people)
    next_key = (rows[-1].occurred_at, rows[-1].id) if more and rows else None
    items = [
        {
            **_event(r),
            "actor_name": (
                names.get(r.actor_user_id) if r.actor_type == "user" and r.actor_user_id else None
            ),
            "review": reviews.get((r.action, r.request_id)),
        }
        for r in rows
    ]
    return HistoryPage(items=items, has_more=more, next_key=next_key)


# Item 17: the origins whose content Kuinn wrote (an automatic fix is a regeneration)
KUINN_ORIGINS: Final = frozenset({"generation", "regeneration"})


def written_by(origin: str) -> str:
    """Who wrote a version's content: Kuinn for a generation or regeneration, else a person (an
    edit, an import)."""
    return "kuinn" if origin in KUINN_ORIGINS else "person"


def _person(user_id: uuid.UUID | None, names: dict[uuid.UUID, str | None]) -> dict[str, Any] | None:
    return None if user_id is None else {"user_id": user_id, "name": names.get(user_id)}


def version_item(v: Any, names: dict[uuid.UUID, str | None]) -> dict[str, Any]:
    return {
        "id": v.id,
        "version": v.version,
        "origin": v.origin,
        "written_by": written_by(v.origin),
        "created_at": v.created_at,
        "generated_by": _person(v.generated_by_user_id, names),
        "edited_by": _person(v.edited_by_user_id, names),
        "approved_by": _person(v.approved_by_user_id, names),
        "approved_at": v.approved_at,
        "self_approved": bool(v.self_approved),
    }


async def versions(
    principal: Principal, listing_id: uuid.UUID, *, after: Keyset | None, limit: int
) -> HistoryPage:
    """Item 17 (client feedback, 11 Nov 2026): who made, edited and approved each version. The
    three user columns reference this tenant's memberships (`to_membership`), and the names are
    read through the tenant-scoped members select, so nothing of another tenant is named."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        if await uow.listings.get(listing_id) is None:
            raise NotFoundError("listing_id")
        rows, more = await uow.listing_versions.page_for_listing(
            listing_id, after=after, limit=limit
        )
        people = {
            u
            for v in rows
            for u in (v.generated_by_user_id, v.edited_by_user_id, v.approved_by_user_id)
            if u is not None
        }
        names = await uow.members.names(people)
    next_key = (rows[-1].created_at, rows[-1].id) if more and rows else None
    return HistoryPage(
        items=[version_item(v, names) for v in rows], has_more=more, next_key=next_key
    )


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
