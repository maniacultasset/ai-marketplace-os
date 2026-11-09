"""The one display state of a listing, and the re-check on read (prod QA 2026-11-09, item 1).

A listing could read `validated` ("Ready to approve") while the marketplace check of its version,
run with today's rules and definitions, blocks it: the verdict was stored before those changed
and nothing asked again. Every screen then disagreed with the Review screen. Two fixes:

- `display_state(listing, needs_you)`: the state every screen shows, computed here so the
  Overview card, the product header, the Products list, Home and the Inbox agree. A listing a
  person could approve (`validated`, `needs_review`) or that a check blocked, with open blocking
  issues on its current version, is `needs_you`; `needs_you_count` is those issues.
- `recheck_stale(uow, principal, listing)`: a reviewable listing whose stored verdict no longer
  matches a check with the current rules, template, facts and marketplace definition is
  validated again (`validate_listing`, the same path as "Run checks"): its status moves, the
  check's issues are synced and its questions open in the Inbox. A check that still matches
  writes nothing. Reads run it on one listing (detail, every time), and on a list's rows: every
  row of a product's list, at most `LIST_RECHECK_CAP` reviewable rows of another list's first
  page. A check costs about 25 queries, so a list skips a row this process checked in the last
  `LIST_RECHECK_TTL_S` seconds with the same version, status and status time (`_checked`); the
  Review screen's detail read is never skipped. The Inbox reads (`recheck_queue`) check the
  review queue the same way, so missing facts behind a stale verdict reach the Inbox.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Final, Literal

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.uow import TenantUnitOfWork

log = get_logger(__name__)

DisplayState = Literal[
    "draft",
    "working",
    "needs_you",
    "needs_review",
    "ready_to_approve",
    "ready_to_list",
    "submitting",
    "published",
    "blocked",
    "error",
]
REVIEWABLE: Final = frozenset({"validated", "needs_review"})
# a person acts next on these; open blocking issues make them "needs you"
NEEDS_YOU_STATUSES: Final = frozenset({"validated", "needs_review", "blocked"})
LIST_RECHECK_CAP: Final = 5
LIST_RECHECK_TTL_S: Final = 120.0
QUEUE_SCAN: Final = 50
_CHECKED_MAX: Final = 4096
# (listing id, current version, status, status_changed_at) -> monotonic time of its last check
_checked: dict[tuple[Any, ...], float] = {}
_BY_STATUS: Final[dict[str, DisplayState]] = {
    "created": "draft",
    "generating": "working",
    "validated": "ready_to_approve",
    "needs_review": "needs_review",
    "approved": "ready_to_list",
    "publishing": "submitting",
    "published": "published",
    "blocked": "blocked",
    "failed": "error",
}


def display_state(listing: Any, needs_you: int) -> DisplayState:
    if listing.status in NEEDS_YOU_STATUSES and needs_you > 0:
        return "needs_you"
    return _BY_STATUS[listing.status]


def display_fields(listing: Any, needs_you: int) -> dict[str, Any]:
    return {"display_state": display_state(listing, needs_you), "needs_you_count": needs_you}


async def needs_you_counts(uow: TenantUnitOfWork, listings: Sequence[Any]) -> dict[Any, int]:
    """Open blocking issues per listing on its current version, one query for the page."""
    return await uow.listing_issues.open_error_counts(
        [(row.id, row.current_version_id) for row in listings]
    )


def _checkable(listing: Any) -> bool:
    return (
        listing.status in REVIEWABLE
        and listing.current_version_id is not None
        and listing.paused_at is None
        and listing.archived_at is None
    )


async def is_stale(uow: TenantUnitOfWork, listing: Any) -> bool:
    """Whether a check now would differ from the stored verdict of the current version: no run
    with today's pins, other "facts changed" items, or other blocking marketplace-check items
    than the open `schema` rows."""
    from app.services.adapter_check import adapter_check
    from app.services.generation_context import previous_listing
    from app.services.listing_validate import same_flags, schema_changed
    from app.services.listings import rules_check

    version = await uow.listing_versions.current(listing)
    canonical = previous_listing(listing, version)
    if version is None or canonical is None:
        return False
    check = await rules_check(uow, listing, canonical)
    pins = check.pins
    same = await uow.validation_runs.find_equal_pins(
        version.id, pins.rules_version, pins.template_id, pins.template_version, pins.facts_digest
    )
    if same is None or not same_flags(same, check.report):
        return True
    adapter = await adapter_check(uow, listing.id)
    return adapter is not None and await schema_changed(uow, listing, adapter)


async def recheck_stale(
    uow: TenantUnitOfWork, principal: Principal, listing: Any, *, wait: bool = True
) -> Any:
    """The listing, validated again first when its stored verdict is stale (module docstring).
    Under the row lock, so two reads at once (Home asks listings, tasks and issues together)
    check once: the second sees the first's verdict and writes nothing. `wait=False` (a list):
    a row another transaction holds is skipped, so reads that lock several rows never wait on
    each other (no deadlock); that transaction is checking or changing it already."""
    from app.services.listing_validate import validate_listing

    if not _checkable(listing):
        return listing
    locked = await uow.listings.get_for_update(listing.id, skip_locked=not wait)
    if locked is None or not _checkable(locked):
        return listing if locked is None else locked
    try:
        if not await is_stale(uow, locked):
            return locked
    except NotFoundError:
        return locked  # the product is gone: the read answers as it is
    await validate_listing(uow, principal, listing.id, None)
    log.info("listing.rechecked_on_read", listing_id=str(listing.id))
    fresh = await uow.listings.get(listing.id)
    return listing if fresh is None else fresh


def _key(listing: Any) -> tuple[Any, ...]:
    return (listing.id, listing.current_version_id, listing.status, listing.status_changed_at)


def _recent(listing: Any, now: float) -> bool:
    at = _checked.get(_key(listing))
    return at is not None and now - at < LIST_RECHECK_TTL_S


def _mark(listing: Any, now: float) -> None:
    if len(_checked) >= _CHECKED_MAX:
        _checked.clear()
    _checked[_key(listing)] = now


async def recheck_queue(uow: TenantUnitOfWork, principal: Principal) -> None:
    """The Inbox reads (open tasks and issues, first page): up to `LIST_RECHECK_CAP` of the
    review queue's oldest `QUEUE_SCAN` listings not checked lately, so a stale "passed" opens its questions and issues before the
    Inbox answers, even when no listing screen was read first (prod QA 9 Nov, item 9)."""
    from app.repositories.listings import ListingFilter

    rows: list[Any] = []
    for status in sorted(REVIEWABLE):
        page, _ = await uow.listings.page(
            ListingFilter(status=status), after=None, limit=QUEUE_SCAN
        )
        rows.extend(page)
    # rows checked lately are skipped, so successive reads move on through a longer queue
    await recheck_page(uow, principal, rows, cap=LIST_RECHECK_CAP)


async def recheck_page(
    uow: TenantUnitOfWork, principal: Principal, rows: Sequence[Any], *, cap: int | None
) -> list[Any]:
    """`recheck_stale` on the reviewable rows not checked lately (at most `cap` of them), in
    place of each row."""
    out = list(rows)
    budget = len(out) if cap is None else cap
    now = time.monotonic()
    for i, row in enumerate(out):
        if budget <= 0:
            break
        if _checkable(row) and not _recent(row, now):
            budget -= 1
            out[i] = await recheck_stale(uow, principal, row, wait=False)
            _mark(out[i], now)
    return out
