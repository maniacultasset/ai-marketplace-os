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
