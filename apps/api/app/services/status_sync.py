"""Pure status-sync rules (S21 T20; DATABASE.md :756, :764, :770; PLAN D7; CP5; T13, T18;
invariant 9). No I/O.

- `next_slot(count)`: the gap to the next counted poll after `count` polls: the DATABASE.md:770
  schedule (+2 m, +10 m, +30 m, +1 h, +3 h, +6 h, +12 h, +24 h, +72 h after the accept) as gaps;
  None after the 9th (exhausted). A live listing is polled once a day, uncounted.
- `aggregate(states)`: the listing's `marketplace_state` from its non-parent items (:756): all live
  → live; ≥ 1 live → partially_live; else the first of processing, pending_review, rejected,
  inactive; all removed → removed; else not_submitted (a published listing never reads
  not_submitted: an unseen item stays processing).
- `sync_decision(...)`: what one sync writes. A SKU the marketplace no longer knows is `removed`
  if it was live, else still `processing`. On a `published` listing: live or partially live →
  `live_*` stamped, the open submission `accepted → live`, the daily poll; rejected →
  `failed/marketplace_rejected` (submission `rejected`), except SI11 T18 (CP-D13): a blocking
  read error of class `account_permission` (only the marketplace can approve the brand or
  category) → `blocked/marketplace_action_required` (the resolution engine then opens the Seller
  Central step and never retries); inactive or removed →
  `blocked/marketplace_action_required` (an open submission `error MARKETPLACE_UNCONFIRMED`),
  except SI9 B3: every non-parent item `inactive` and read `not_buyable` (on the marketplace, no
  offer or stock) stays `published/inactive` with the submission still `accepted`, polled on the
  bounded gaps, then daily and uncounted (never `sync_exhausted`);
  still processing → a counted poll, the 9th → `sync_exhausted` (blocked, submission
  `error MARKETPLACE_UNCONFIRMED`, `next_sync_at` NULL). On `failed/publish_partial` (D7) only the
  items and `marketplace_state` change, polled on the same bounded schedule. Anything else (a
  publish in flight, a blocked or paused listing) writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Literal

from mkos_schemas.enums import ErrorClass, Severity
from mkos_schemas.errors import ErrorItem

MAX_POLLS: Final = 9
DELTAS: Final[tuple[timedelta, ...]] = (
    timedelta(minutes=2),
    timedelta(minutes=8),
    timedelta(minutes=20),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=3),
    timedelta(hours=6),
    timedelta(hours=12),
    timedelta(hours=48),
)
DAILY: Final = timedelta(hours=24)
ORDER: Final = ("processing", "pending_review", "rejected", "inactive")
WAITING: Final = frozenset({"processing", "pending_review"})

Event = Literal["sync", "marketplace_rejected", "marketplace_action_required", "sync_exhausted"]
SubmissionTo = Literal["live", "rejected", "error"]


def next_slot(count: int) -> timedelta | None:
    if count < 0:
        raise ValueError("count must be >= 0")
    return DELTAS[count] if count < MAX_POLLS else None


def aggregate(states: Sequence[str]) -> str:
    if not states:
        return "not_submitted"
    if all(s == "live" for s in states):
        return "live"
    if any(s == "live" for s in states):
        return "partially_live"
    for state in ORDER:
        if state in states:
            return state
    if all(s == "removed" for s in states):
        return "removed"
    return "not_submitted"


@dataclass(frozen=True, slots=True)
class ItemRead:
    """One SKU as read: its state (None = unknown to the marketplace) and external id."""

    seller_sku: str
    state: str | None
    external_id: str | None = None
    parent: bool = False
    # SI4 A16: the read's field errors (mapped by the adapter) and the paths it read
    errors: tuple[ErrorItem, ...] = ()
    fields: tuple[str, ...] = ()
    # SI9 B3: the adapter read the product on the marketplace, not buyable yet
    not_buyable: bool = False
    # SI13: the product type the marketplace files the item under (the reassignment signal)
    product_type: str | None = None


@dataclass(frozen=True, slots=True)
class SyncDecision:
    items: Mapping[str, str] = field(default_factory=dict)
    marketplace_state: str | None = None
    event: Event | None = None
    listing_to: str | None = None
    reason: str | None = None
    counted: bool = False
    poll_count: int = 0
    next_in: timedelta | None = None
    stamp_live: bool = False
    submission_to: SubmissionTo | None = None
    not_buyable: bool = False


def _item_state(read: ItemRead, previous: str | None) -> str:
    if read.state is None:
        return "removed" if previous == "live" else "processing"
    return read.state


def sync_decision(
    *,
    status: str,
    reason: str | None,
    current_state: str,
    poll_count: int,
    reads: Sequence[ItemRead],
    previous: Mapping[str, str],
    submission_open: bool,
) -> SyncDecision:
    partial = status == "failed" and reason == "publish_partial"
    if status != "published" and not partial:
        return SyncDecision(poll_count=poll_count)
    items = {r.seller_sku: _item_state(r, previous.get(r.seller_sku)) for r in reads}
    states = [items[r.seller_sku] for r in reads if not r.parent] or list(items.values())
    agg = aggregate(states)
    if partial:
        agg = "processing" if agg in ("removed", "not_submitted") else agg
        if agg in ("live", "partially_live"):
            return SyncDecision(items, agg, poll_count=poll_count, next_in=DAILY, stamp_live=True)
        count = min(poll_count + 1, MAX_POLLS)
        return SyncDecision(items, agg, counted=True, poll_count=count, next_in=next_slot(count))
    if agg in ("live", "partially_live"):
        return SyncDecision(
            items,
            agg,
            event="sync" if agg != current_state else None,
            poll_count=poll_count,
            next_in=DAILY,
            stamp_live=True,
            submission_to="live" if submission_open and agg == "live" else None,
        )
    if agg == "rejected" and _needs_permission(reads):
        return SyncDecision(
            items,
            "rejected",
            event="marketplace_action_required",
            listing_to="blocked",
            reason="marketplace_action_required",
            submission_to="rejected" if submission_open else None,
        )
    if agg == "rejected":
        return SyncDecision(
            items,
            "rejected",
            event="marketplace_rejected",
            listing_to="failed",
            reason="marketplace_rejected",
            submission_to="rejected" if submission_open else None,
        )
    considered = [r for r in reads if not r.parent] or list(reads)
    if agg == "inactive" and all(
        r.not_buyable and items[r.seller_sku] == "inactive" for r in considered
    ):
        return _not_buyable(items, current_state, poll_count)
    if agg in ("inactive", "removed"):
        return SyncDecision(
            items,
            agg,
            event="marketplace_action_required",
            listing_to="blocked",
            reason="marketplace_action_required",
            submission_to="error" if submission_open else None,
        )
    # still waiting (processing / pending_review / never seen): a counted poll
    agg = agg if agg in WAITING else "processing"
    # clamped: a not-buyable or live listing may already sit at 9 (the 0..9 CHECK)
    count = min(poll_count + 1, MAX_POLLS)
    if count >= MAX_POLLS:
        return SyncDecision(
            items,
            agg,
            event="sync_exhausted",
            listing_to="blocked",
            reason="marketplace_action_required",
            counted=True,
            poll_count=count,
            next_in=None,
            submission_to="error" if submission_open else None,
        )
    return SyncDecision(
        items,
        agg,
        event="sync" if agg != current_state else None,
        counted=True,
        poll_count=count,
        next_in=next_slot(count),
    )


def _needs_permission(reads: Sequence[ItemRead]) -> bool:
    """T18: a blocking read error only the marketplace can clear (an account or category
    permission)."""
    from app.services.publish_ledger import item_class

    return any(
        e.severity is Severity.ERROR and item_class(e) is ErrorClass.ACCOUNT_PERMISSION
        for r in reads
        for e in r.errors
    )


def _not_buyable(items: Mapping[str, str], current_state: str, poll_count: int) -> SyncDecision:
    """SI9 B3 (D3): published and re-checked. The bounded counted polls first (the offer may be
    on its way), then a daily uncounted poll, never `sync_exhausted`; the poll count stays within
    the 0..9 CHECK."""
    if poll_count < MAX_POLLS:
        count, counted = poll_count + 1, True
        next_in = next_slot(count) or DAILY
    else:
        count, counted, next_in = poll_count, False, DAILY
    return SyncDecision(
        items,
        "inactive",
        event="sync" if current_state != "inactive" else None,
        counted=counted,
        poll_count=count,
        next_in=next_in,
        not_buyable=True,
    )
