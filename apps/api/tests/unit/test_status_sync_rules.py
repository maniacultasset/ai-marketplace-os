"""S21 T20 (AC10; D7; CP5; DATABASE.md :756, :764, :770): the pure status-sync rules."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from app.services.status_sync import (
    DAILY,
    MAX_POLLS,
    ItemRead,
    aggregate,
    next_slot,
    sync_decision,
)
from hypothesis import given
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem

GAPS_MIN = [2, 8, 20, 30, 120, 180, 360, 720, 2880]


def test_next_slot_table() -> None:
    assert [int(next_slot(n).total_seconds() // 60) for n in range(9)] == GAPS_MIN  # type: ignore[union-attr]
    # cumulative: the DATABASE.md:770 schedule +2 m, +10 m, +30 m, +1 h, +3 h, +6 h, +12 h,
    # +24 h, +72 h after the accept
    totals = [sum(GAPS_MIN[: n + 1]) for n in range(9)]
    assert totals == [2, 10, 30, 60, 180, 360, 720, 1440, 4320]
    assert next_slot(9) is None
    with pytest.raises(ValueError):
        next_slot(-1)


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["live", "live"], "live"),
        (["live", "processing"], "partially_live"),
        (["processing", "rejected"], "processing"),
        (["pending_review", "rejected"], "pending_review"),
        (["rejected", "inactive"], "rejected"),
        (["inactive", "removed"], "inactive"),
        (["removed", "removed"], "removed"),
        (["not_submitted"], "not_submitted"),
        ([], "not_submitted"),
    ],
)
def test_aggregate_golden(states: list[str], expected: str) -> None:
    assert aggregate(states) == expected


def _decide(
    reads: list[ItemRead],
    *,
    status: str = "published",
    reason: str | None = None,
    current: str = "processing",
    polls: int = 0,
    previous: dict[str, str] | None = None,
    open_: bool = True,
) -> Any:
    return sync_decision(
        status=status,
        reason=reason,
        current_state=current,
        poll_count=polls,
        reads=reads,
        previous=previous or {},
        submission_open=open_,
    )


def test_status_sync_decision_live() -> None:
    d = _decide([ItemRead("A", "live", "B01")])
    assert (d.marketplace_state, d.event, d.submission_to, d.next_in, d.counted) == (
        "live",
        "sync",
        "live",
        DAILY,
        False,
    )
    assert d.stamp_live and d.listing_to is None


def test_status_sync_decision_half_live() -> None:
    reads = [
        ItemRead("P", "processing", parent=True),
        ItemRead("A", "live"),
        ItemRead("B", "processing"),
    ]
    d = _decide(reads)
    assert d.marketplace_state == "partially_live" and d.submission_to is None


def test_status_sync_decision_rejected() -> None:
    d = _decide([ItemRead("A", "rejected")])
    assert (d.listing_to, d.reason, d.event, d.submission_to) == (
        "failed",
        "marketplace_rejected",
        "marketplace_rejected",
        "rejected",
    )


def _read_error(code: ErrorCode) -> ErrorItem:
    return ErrorItem(
        field="listing",
        error_code=code,
        severity=Severity.ERROR,
        message="The marketplace refused this listing.",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
    )


def test_status_sync_decision_permission_needs_the_marketplace() -> None:
    """SI11 T18: an account-permission read error is a marketplace step, never a rejection."""
    error = _read_error(ErrorCode.ACCOUNT_PERMISSION_DENIED)
    d = _decide([ItemRead("A", "rejected", errors=(error,))])
    assert (d.listing_to, d.reason, d.event, d.submission_to) == (
        "blocked",
        "marketplace_action_required",
        "marketplace_action_required",
        "rejected",
    )


def test_status_sync_decision_invalid_attribute_stays_rejected() -> None:
    error = _read_error(ErrorCode.VALUE_NOT_ALLOWED)
    d = _decide([ItemRead("A", "rejected", errors=(error,))])
    assert (d.listing_to, d.event) == ("failed", "marketplace_rejected")


@pytest.mark.parametrize("state", ["inactive", None])
def test_status_sync_decision_inactive_or_removed(state: str | None) -> None:
    d = _decide([ItemRead("A", state)], previous={"A": "live"}, current="live")
    assert (d.listing_to, d.reason, d.event) == (
        "blocked",
        "marketplace_action_required",
        "marketplace_action_required",
    )


def test_status_sync_decision_processing_counts() -> None:
    d = _decide([ItemRead("A", "processing")], polls=3)
    assert (d.counted, d.poll_count, d.next_in, d.event) == (True, 4, timedelta(hours=2), None)


def test_status_sync_decision_unseen_stays_processing() -> None:
    d = _decide([ItemRead("A", None)])
    assert d.marketplace_state == "processing" and d.listing_to is None


def test_status_sync_decision_ninth_processing_exhausted() -> None:
    d = _decide([ItemRead("A", "processing")], polls=MAX_POLLS - 1)
    assert (d.event, d.listing_to, d.reason, d.next_in, d.submission_to) == (
        "sync_exhausted",
        "blocked",
        "marketplace_action_required",
        None,
        "error",
    )


def test_status_sync_decision_live_daily_uncounted() -> None:
    d = _decide([ItemRead("A", "live")], current="live", polls=2, open_=False)
    assert (d.event, d.counted, d.poll_count, d.next_in, d.submission_to) == (
        None,
        False,
        2,
        DAILY,
        None,
    )


def test_status_sync_decision_partial_listing_items_only() -> None:
    d = _decide([ItemRead("A", "live")], status="failed", reason="publish_partial")
    assert d.listing_to is None and d.event is None and d.marketplace_state == "live"
    d = _decide([ItemRead("A", "processing")], status="failed", reason="publish_partial")
    assert d.listing_to is None and d.counted


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("publishing", None),
        ("blocked", "marketplace_action_required"),
        ("failed", "publish_failed"),
    ],
)
def test_status_sync_decision_skips_other_statuses(status: str, reason: str | None) -> None:
    d = _decide([ItemRead("A", "live")], status=status, reason=reason)
    assert d.marketplace_state is None and d.listing_to is None


@given(st.integers(0, 50), st.sampled_from(["processing", "pending_review", None]))
def test_count_never_exceeds_9(polls: int, state: str | None) -> None:
    d = _decide([ItemRead("A", state)], polls=min(polls, MAX_POLLS - 1))
    assert d.poll_count <= MAX_POLLS


# ---- SI9 T6 (AC4, AC5): on the marketplace, not buyable ----


def _nb(sku: str = "S1", **over: Any) -> ItemRead:
    return ItemRead(sku, "inactive", external_id="B0X", not_buyable=True, **over)


def test_not_buyable_first_read_stays_published() -> None:
    d = _decide([_nb()], current="processing", polls=0)
    assert (d.marketplace_state, d.event, d.listing_to, d.submission_to) == (
        "inactive",
        "sync",
        None,
        None,
    )
    assert (d.counted, d.poll_count, d.next_in, d.not_buyable) == (True, 1, next_slot(1), True)


def test_not_buyable_same_state_no_event() -> None:
    d = _decide([_nb()], current="inactive", polls=3)
    assert d.event is None and d.listing_to is None
    assert (d.counted, d.poll_count) == (True, 4)


def test_not_buyable_ninth_poll_goes_daily() -> None:
    d = _decide([_nb()], current="inactive", polls=MAX_POLLS - 1)
    assert (d.counted, d.poll_count, d.next_in) == (True, MAX_POLLS, DAILY)
    assert d.event is None and d.listing_to is None


def test_not_buyable_after_nine_polls_daily_uncounted() -> None:
    d = _decide([_nb()], current="inactive", polls=MAX_POLLS)
    assert (d.counted, d.poll_count, d.next_in) == (False, MAX_POLLS, DAILY)
    assert d.listing_to is None and d.event is None


def test_mixed_not_buyable_and_plain_inactive_blocks() -> None:
    d = _decide([_nb("S1"), ItemRead("S2", "inactive")])
    assert (d.listing_to, d.reason) == ("blocked", "marketplace_action_required")
    assert not d.not_buyable


def test_not_buyable_child_and_processing_child_is_processing() -> None:
    d = _decide([_nb("S1"), ItemRead("S2", "processing")])
    assert (d.marketplace_state, d.counted, d.listing_to, d.not_buyable) == (
        "processing",
        True,
        None,
        False,
    )


def test_not_buyable_parent_and_live_child_is_live() -> None:
    d = _decide([_nb("P", parent=True), ItemRead("C", "live")])
    assert d.marketplace_state == "live" and d.stamp_live


def test_live_then_not_buyable_reads_inactive() -> None:
    d = _decide([_nb()], current="live", previous={"S1": "live"})
    assert (d.marketplace_state, d.event, d.listing_to, d.submission_to) == (
        "inactive",
        "sync",
        None,
        None,
    )


def test_partial_with_not_buyable_read_keeps_partial_branch() -> None:
    d = _decide([_nb()], status="failed", reason="publish_partial", polls=2)
    assert (d.marketplace_state, d.counted, d.poll_count, d.listing_to) == (
        "inactive",
        True,
        3,
        None,
    )
    assert not d.not_buyable


@pytest.mark.parametrize("read", [ItemRead("S1", "inactive"), ItemRead("S1", "removed")])
def test_plain_inactive_or_removed_still_blocks(read: ItemRead) -> None:
    d = _decide([read])
    assert (d.listing_to, d.reason, d.submission_to) == (
        "blocked",
        "marketplace_action_required",
        "error",
    )


@pytest.mark.parametrize("current", ["inactive", "live"])
def test_waiting_read_after_nine_polls_exhausts_within_check(current: str) -> None:
    # a not-buyable (or later live) listing parked at 9 that then reads processing
    d = _decide(
        [ItemRead("S1", "processing")], current=current, polls=MAX_POLLS, previous={"S1": current}
    )
    assert (d.event, d.listing_to, d.poll_count) == ("sync_exhausted", "blocked", MAX_POLLS)


def test_partial_after_nine_polls_stays_within_check() -> None:
    d = _decide(
        [ItemRead("S1", "processing")], status="failed", reason="publish_partial", polls=MAX_POLLS
    )
    assert d.poll_count <= MAX_POLLS


def test_daily_slot_is_spread_over_an_hour() -> None:
    """V1 hardening #8: a daily (or longer) slot gets up to one hour of jitter; the short polls
    after a publish keep their exact times."""
    from datetime import timedelta

    from app.services.status_sync import SYNC_JITTER, spread

    assert timedelta(hours=1) == SYNC_JITTER
    assert spread(DAILY, 0.0) == DAILY
    assert spread(DAILY, 0.5) == DAILY + timedelta(minutes=30)
    assert spread(timedelta(minutes=2), 0.9) == timedelta(minutes=2)
    for _ in range(50):
        assert DAILY <= spread(DAILY) < DAILY + SYNC_JITTER
    with pytest.raises(ValueError):
        spread(DAILY, 1.0)
