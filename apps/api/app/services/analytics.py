"""Operational analytics (S23; spec L4773, L5370; TEAM-QUESTIONS T14: operational metrics only).

- `resolve_range(from_, to, today=)`: the inclusive UTC-day range a request asks for. Both absent:
  the 30 days ending today; only `to`: the 30 days ending `to`; only `from`: `from` to today.
  `from` after `to`, or more than 366 days inclusive, raises `AnalyticsRangeError` (422 at the
  router). A future `to` is allowed.
- `DateRange.bounds()`: the half-open UTC instants `[start 00:00Z, end+1 00:00Z)`.
- `rate(num, den)`: `num / den` as a `Decimal` rounded half-even to 4 places, None when `den` is 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

DEFAULT_DAYS: Final = 30
MAX_DAYS: Final = 366
RATE_QUANTUM: Final = Decimal("0.0001")


class AnalyticsRangeError(ValueError):
    """A request range the endpoint refuses; `field` names the query parameter at fault."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(reason)
        self.field = field
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    def bounds(self) -> tuple[datetime, datetime]:
        lo = datetime.combine(self.start, time.min, tzinfo=UTC)
        hi = datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=UTC)
        return lo, hi


def resolve_range(from_: date | None, to: date | None, *, today: date) -> DateRange:
    if to is None:
        to = today
    if from_ is None:
        from_ = to - timedelta(days=DEFAULT_DAYS - 1)
    if from_ > to:
        raise AnalyticsRangeError("from", "'from' must not be after 'to'.")
    if (to - from_).days + 1 > MAX_DAYS:
        raise AnalyticsRangeError("to", f"The range may span at most {MAX_DAYS} days.")
    return DateRange(from_, to)


def rate(num: int, den: int) -> Decimal | None:
    if num < 0 or den < 0 or num > den:
        raise ValueError("a rate needs 0 <= num <= den")
    if den == 0:
        return None
    return (Decimal(num) / Decimal(den)).quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)
