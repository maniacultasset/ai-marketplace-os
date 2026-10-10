"""Operational analytics (S23; spec L4773, L5370; TEAM-QUESTIONS T14: operational metrics only).

- `resolve_range(from_, to, today=)`: the inclusive UTC-day range a request asks for. Both absent:
  the 30 days ending today; only `to`: the 30 days ending `to`; only `from`: `from` to today.
  `from` after `to`, or more than 366 days inclusive, raises `AnalyticsRangeError` (422 at the
  router). A future `to` is allowed. A range Python's calendar cannot hold (`to` on 9999-12-31,
  whose end bound is the next day, or a default 30-day start before 0001-01-01) is refused the
  same way at `to`, before any date arithmetic (audit VALIDATION-1).
- `DateRange.bounds()`: the half-open UTC instants `[start 00:00Z, end+1 00:00Z)`.
- `rate(num, den)`: `num / den` as a `Decimal` rounded half-even to 4 places, None when `den` is 0.
- `summarize(repo, period)`: the `AnalyticsSummary` body (formulas: `app/repositories/analytics.py`).
- `inflight(tenant_id)`: at most `INFLIGHT_CAP` analytics requests per tenant at once (bucket
  `an:<tenant_id>` on `rate_limit_counters`); a third raises `AnalyticsBusyError` (429 at the
  router). The slot is given back whatever the request did.
- `get_summary(...)`: the endpoint's work: the range, the slot, one READ ONLY transaction under one
  5 s budget (`AnalyticsBudgetError` when it runs out), the summary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, astuple, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final
from uuid import UUID

from mkos_schemas.enums import Marketplace

from app.repositories import rate_limits
from app.repositories.analytics import AnalyticsBudgetError, AnalyticsRepository
from app.repositories.transactions import unit_of_work

DEFAULT_DAYS: Final = 30
MAX_DAYS: Final = 366
RATE_QUANTUM: Final = Decimal("0.0001")
INFLIGHT_CAP: Final = 2

__all__ = [
    "AnalyticsBudgetError",
    "AnalyticsBusyError",
    "AnalyticsRangeError",
    "Counts",
    "DateRange",
    "get_summary",
    "inflight",
    "rate",
    "resolve_range",
    "summarize",
]


class AnalyticsBusyError(Exception):
    """The tenant already has `INFLIGHT_CAP` analytics requests in flight."""


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


_LATEST_TO: Final = date.max - timedelta(days=1)
"""`bounds()` needs `to + 1 day`."""
_EARLIEST_DEFAULT_TO: Final = date.min + timedelta(days=DEFAULT_DAYS - 1)
"""Without `from`, the range starts `DEFAULT_DAYS - 1` days before `to`."""
_OUT_OF_RANGE: Final = "Date out of range."


def resolve_range(from_: date | None, to: date | None, *, today: date) -> DateRange:
    if to is None:
        to = today
    if to > _LATEST_TO or (from_ is None and to < _EARLIEST_DEFAULT_TO):
        raise AnalyticsRangeError("to", _OUT_OF_RANGE)
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


# ---- the summary (T5) ----

MARKETPLACE_ORDER: Final[tuple[str, ...]] = tuple(m.value for m in Marketplace)


@dataclass(frozen=True, slots=True)
class Counts:
    listings_generated: int = 0
    approvals: int = 0
    rejections: int = 0
    validation_first_pass_passed: int = 0
    validation_first_pass_total: int = 0
    publish_succeeded: int = 0
    publish_failed: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(*(a + b for a, b in zip(astuple(self), astuple(other), strict=True)))

    def any(self) -> bool:
        return any(astuple(self))

    def rates(self) -> dict[str, float | None]:
        return {
            "approval_rate": _wire(rate(self.approvals, self.approvals + self.rejections)),
            "validation_pass_rate": _wire(
                rate(self.validation_first_pass_passed, self.validation_first_pass_total)
            ),
            "publish_success_rate": _wire(
                rate(self.publish_succeeded, self.publish_succeeded + self.publish_failed)
            ),
        }


def _wire(value: Decimal | None) -> float | None:
    """A 4-place rate as the JSON number it serialises to."""
    return None if value is None else float(value)


def _block(counts: Counts) -> dict[str, Any]:
    return {"listings_generated": counts.listings_generated, **counts.rates()}


async def summarize(repo: AnalyticsRepository, period: DateRange) -> dict[str, Any]:
    """The `AnalyticsSummary` body for `period`: totals are the sums of the per-marketplace
    counts, and `by_marketplace` lists only marketplaces with any count, in enum order."""
    lo, hi = period.bounds()
    volume = await repo.volume(lo, hi)
    decisions = await repo.decisions(lo, hi)
    validation = await repo.validation(lo, hi)
    publish = await repo.publish(lo, hi)
    per: dict[str, Counts] = {}
    for marketplace in MARKETPLACE_ORDER:
        approvals, rejections = decisions.get(marketplace, (0, 0))
        passed, total = validation.get(marketplace, (0, 0))
        succeeded, failed = publish.get(marketplace, (0, 0))
        counts = Counts(
            volume.get(marketplace, 0),
            approvals,
            rejections,
            passed,
            total,
            succeeded,
            failed,
        )
        if counts.any():
            per[marketplace] = counts
    unknown = (set(volume) | set(decisions) | set(validation) | set(publish)) - set(
        MARKETPLACE_ORDER
    )
    if unknown:
        raise ValueError(f"analytics rows on unknown marketplace(s) {sorted(unknown)}")
    totals = sum(per.values(), Counts())
    return {
        "from": period.start,
        "to": period.end,
        **_block(totals),
        "counts": asdict(totals),
        "by_marketplace": [
            {"marketplace": m, **_block(c), "counts": asdict(c)} for m, c in per.items()
        ],
    }


def utc_now() -> datetime:
    return datetime.now(UTC)


@asynccontextmanager
async def inflight(tenant_id: UUID) -> AsyncIterator[None]:
    bucket = f"an:{tenant_id}"
    window = await rate_limits.inflight_acquire(bucket, INFLIGHT_CAP, utc_now())
    if window is None:
        raise AnalyticsBusyError(bucket)
    try:
        yield
    finally:
        await rate_limits.inflight_release(bucket, window)


async def get_summary(
    tenant_id: UUID, user_id: UUID | None, from_: date | None, to: date | None
) -> dict[str, Any]:
    period = resolve_range(from_, to, today=utc_now().date())
    async with inflight(tenant_id), unit_of_work(tenant_id, user_id=user_id) as session:
        repo = AnalyticsRepository(session, tenant_id)
        await repo.bounded()
        return await summarize(repo, period)
