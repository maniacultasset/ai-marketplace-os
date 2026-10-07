"""The ops numbers behind the admin's ops pages (S14b T21; BUILD-SEQUENCE.md:235; KNOWN-ISSUES.md:31).

Core SELECTs on a `platform_scope()` session (read-only, `app_platform_ro`; R5 allows it here),
each returning frozen dataclasses. Money stays `Decimal` from `agent_runs.cost_usd`; days are UTC.

- `queue_stats(now)`: per queue, depth (`queued` + `running`), the oldest `queued` age, and the
  dead-letter count.
- `dead_letters(limit=50)`: the newest dead letters with the tenant slug; no payload, no result.
- `daily_spend(now, days=7)`: AI spend per UTC day, in total and per tenant.
- `cost_per_published_listing(now)`: 30-day spend over listings first published in those 30 days
  (`None` with none published).
- `funnel(now)`: listings by status whose status changed in the last 24 hours.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import Date, cast, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models.listings import Listing
from app.db.models.ops import JobRun
from app.db.models.runs import AgentRun
from app.db.models.tenancy import Tenant

DEAD_LETTER_LIMIT: Final = 50
COST_WINDOW: Final = timedelta(days=30)
FUNNEL_WINDOW: Final = timedelta(hours=24)
MONEY: Final = Decimal("0.000001")


@dataclass(frozen=True)
class QueueStat:
    queue: str
    depth: int
    oldest_queued_seconds: int | None
    dead_letters: int


@dataclass(frozen=True)
class DeadLetter:
    id: UUID
    tenant_slug: str
    kind: str
    error_code: str | None
    finished_at: datetime | None


@dataclass(frozen=True)
class DaySpend:
    day: date
    total: Decimal
    per_tenant: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class CostPerListing:
    spend: Decimal
    published: int
    per_listing: Decimal | None


@dataclass(frozen=True)
class FunnelRow:
    status: str
    count: int


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


async def queue_stats(session: AsyncSession, now: datetime) -> list[QueueStat]:
    current = _aware(now)
    live = JobRun.status.in_(("queued", "running"))
    stmt = (
        select(
            JobRun.queue,
            func.count().filter(live),
            func.min(JobRun.queued_at).filter(JobRun.status == "queued"),
            func.count().filter(JobRun.status == "dead_letter"),
        )
        .group_by(JobRun.queue)
        .order_by(JobRun.queue)
    )
    out: list[QueueStat] = []
    for queue, depth, oldest, dead in (await session.execute(stmt)).all():
        age = None if oldest is None else max(0, int((current - oldest).total_seconds()))
        out.append(QueueStat(queue, int(depth), age, int(dead)))
    return out


async def dead_letters(session: AsyncSession, limit: int = DEAD_LETTER_LIMIT) -> list[DeadLetter]:
    if isinstance(limit, bool) or not 1 <= limit <= DEAD_LETTER_LIMIT:
        raise ValueError(f"limit must be in 1..{DEAD_LETTER_LIMIT}")
    stmt = (
        select(JobRun.id, Tenant.slug, JobRun.kind, JobRun.error_code, JobRun.finished_at)
        .join(Tenant, Tenant.id == JobRun.tenant_id)
        .where(JobRun.status == "dead_letter")
        .order_by(desc(JobRun.finished_at), JobRun.id)
        .limit(limit)
    )
    return [DeadLetter(*row) for row in (await session.execute(stmt)).all()]


def _utc_day(column: ColumnElement[datetime]) -> ColumnElement[date]:
    return cast(func.timezone("UTC", column), Date)


async def daily_spend(session: AsyncSession, now: datetime, days: int = 7) -> list[DaySpend]:
    if isinstance(days, bool) or not 1 <= days <= 92:
        raise ValueError("days must be in 1..92")
    today = _aware(now).date()
    first = today - timedelta(days=days - 1)
    start = datetime(first.year, first.month, first.day, tzinfo=UTC)
    day = _utc_day(AgentRun.created_at)  # type: ignore[arg-type]
    stmt = (
        select(day, Tenant.slug, func.sum(AgentRun.cost_usd))
        .join(Tenant, Tenant.id == AgentRun.tenant_id)
        .where(AgentRun.created_at >= start)
        .group_by(day, Tenant.slug)
        .order_by(day, Tenant.slug)
    )
    totals: dict[date, dict[str, Decimal]] = {first + timedelta(days=n): {} for n in range(days)}
    for spent_day, slug, total in (await session.execute(stmt)).all():
        if spent_day in totals:
            totals[spent_day][slug] = Decimal(total).quantize(MONEY)
    return [
        DaySpend(d, sum(per.values(), Decimal("0")).quantize(MONEY), per)
        for d, per in sorted(totals.items())
    ]


async def cost_per_published_listing(session: AsyncSession, now: datetime) -> CostPerListing:
    since = _aware(now) - COST_WINDOW
    spend_stmt = select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(
        AgentRun.created_at >= since
    )
    published_stmt = select(func.count()).where(Listing.first_published_at >= since)
    spend = Decimal((await session.execute(spend_stmt)).scalar_one()).quantize(MONEY)
    published = int((await session.execute(published_stmt)).scalar_one())
    per = None if published == 0 else (spend / published).quantize(MONEY)
    return CostPerListing(spend, published, per)


async def funnel(session: AsyncSession, now: datetime) -> list[FunnelRow]:
    since = _aware(now) - FUNNEL_WINDOW
    stmt = (
        select(Listing.status, func.count())
        .where(Listing.status_changed_at >= since)
        .group_by(Listing.status)
        .order_by(Listing.status)
    )
    return [FunnelRow(status, int(n)) for status, n in (await session.execute(stmt)).all()]


__all__ = [
    "CostPerListing",
    "DaySpend",
    "DeadLetter",
    "FunnelRow",
    "QueueStat",
    "cost_per_published_listing",
    "daily_spend",
    "dead_letters",
    "funnel",
    "queue_stats",
]
