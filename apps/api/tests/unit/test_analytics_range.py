"""S23 T3: the analytics date range (C5) and rate arithmetic (C4), pure."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from app.services.analytics import AnalyticsRangeError, DateRange, rate, resolve_range
from hypothesis import given
from hypothesis import strategies as st

TODAY = date(2026, 10, 9)


def test_default_last_30_days() -> None:
    r = resolve_range(None, None, today=TODAY)
    assert r == DateRange(date(2026, 9, 10), TODAY)
    assert (r.end - r.start).days == 29


def test_default_only_to() -> None:
    assert resolve_range(None, date(2026, 1, 31), today=TODAY) == DateRange(
        date(2026, 1, 2), date(2026, 1, 31)
    )


def test_default_only_from() -> None:
    assert resolve_range(date(2026, 10, 1), None, today=TODAY) == DateRange(
        date(2026, 10, 1), TODAY
    )


def test_from_after_to_422() -> None:
    with pytest.raises(AnalyticsRangeError) as caught:
        resolve_range(date(2026, 10, 2), date(2026, 10, 1), today=TODAY)
    assert caught.value.field == "from"


def test_only_from_in_future_is_after_today() -> None:
    with pytest.raises(AnalyticsRangeError):
        resolve_range(date(2026, 10, 10), None, today=TODAY)


def test_span_366_inclusive_ok() -> None:
    start = date(2025, 1, 1)
    r = resolve_range(start, start + timedelta(days=365), today=TODAY)
    assert (r.end - r.start).days == 365


def test_span_367_refused() -> None:
    start = date(2025, 1, 1)
    with pytest.raises(AnalyticsRangeError) as caught:
        resolve_range(start, start + timedelta(days=366), today=TODAY)
    assert caught.value.field == "to"


def test_same_day_ok() -> None:
    assert resolve_range(TODAY, TODAY, today=TODAY) == DateRange(TODAY, TODAY)


def test_future_to_ok() -> None:
    later = TODAY + timedelta(days=5)
    assert resolve_range(TODAY, later, today=TODAY).end == later


def test_bounds_utc_half_open() -> None:
    lo, hi = DateRange(TODAY, TODAY).bounds()
    assert lo == datetime(2026, 10, 9, tzinfo=UTC)
    assert hi == datetime(2026, 10, 10, tzinfo=UTC)
    last = datetime(2026, 10, 9, 23, 59, 59, 999999, tzinfo=UTC)
    assert lo <= last < hi
    assert not (datetime(2026, 10, 10, tzinfo=UTC) < hi)


def test_rate_zero_den_none() -> None:
    assert rate(0, 0) is None


@pytest.mark.parametrize(
    ("num", "den", "want"),
    [
        (1, 3, "0.3333"),
        (2, 3, "0.6667"),
        (1, 8, "0.1250"),
        (5, 80000, "0.0001"),  # 0.0000625 rounds up
        (4, 80000, "0.0000"),  # 0.00005 exactly: half-even goes to 0
        (12, 80000, "0.0002"),  # 0.00015 exactly: half-even goes to 2
        (3, 3, "1.0000"),
        (0, 7, "0.0000"),
    ],
)
def test_rate_half_even_4dp(num: int, den: int, want: str) -> None:
    assert rate(num, den) == Decimal(want)


@given(
    st.integers(min_value=1, max_value=10**9).flatmap(
        lambda d: st.tuples(st.integers(0, d), st.just(d))
    )
)
def test_rate_within_0_1(pair: tuple[int, int]) -> None:
    num, den = pair
    value = rate(num, den)
    assert value is not None and Decimal(0) <= value <= Decimal(1)


@pytest.mark.parametrize(("num", "den"), [(-1, 3), (4, 3), (1, -1)])
def test_rate_refuses_out_of_range_counts(num: int, den: int) -> None:
    with pytest.raises(ValueError):
        rate(num, den)
