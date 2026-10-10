"""The cross-tenant rollups (S23; KNOWN-ISSUES.md:31; DATABASE.md:1031): the one worker module,
besides the periodic slots and the budget claim, allowed `platform_scope()` (R5).

Every function takes a session from `rollup_session()` (`platform_scope()`: READ ONLY as
`app_platform_ro`, never commits) and runs Core SELECTs (no raw SQL), returning frozen
dataclasses. Money stays `Decimal` from `agent_runs.cost_usd` (`numeric(12,6)`, summed exactly);
days are UTC days of `agent_runs.started_at`, the column the kill switch sums
(`app/repositories/budget.py`) and the 0002 `(tenant_id, started_at) INCLUDE (cost_usd)` index
covers (an IST offset never moves a row).

Cost (spec L12705; BACKEND-ARCHITECTURE.md:2761):
- `daily_spend(s, now, days=7)` and `cost_per_published_listing(s, now)`: the staff cost page's
  numbers (moved from `app/admin/ops.py`, same goldens).
- `spend_today(s, now)`: today's spend and live `listing_generate` reservations, global and per
  tenant, with each tenant's cap (`tenants.llm_daily_budget_usd`, else the default): the same
  numbers the kill switch refuses on (`app/repositories/budget.py`), for the budget alerts.
- `cost_per_listing(s, now, days=30, limit=50)`: the top listings by 30-day cost (cost desc, id asc).
- `listing_cost(s, listing_id)`: one listing's all-time cost, or None for an unknown listing.
- `cost_by_model(s, day)`, `cost_by_trigger(s, day)`: one UTC day's spend per model and trigger.

Jobs (R12661a; the alert checks):
- `job_outcomes(s, now, window=15 min)`: per job kind, terminal and failed counts finished in the
  window (failed = `failed` or `dead_letter`).
- `oldest_queued(s, now)`: per queue, the age of the oldest `queued` job (None when empty).
- `processing_time(s, now, window)`: per job kind, `finished_at - started_at` count, mean and max.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from mkos_schemas.enums import JobKind
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import get_settings
from app.db.models.listings import Listing
from app.db.models.ops import JobRun
from app.db.models.runs import AgentRun
from app.db.models.tenancy import Tenant
from app.db.session import platform_scope

COST_WINDOW: Final = timedelta(days=30)
MONEY: Final = Decimal("0.000001")
ZERO: Final = Decimal("0")
TOP_LISTINGS: Final = 50
JOB_WINDOW: Final = timedelta(minutes=15)
QUEUES: Final[tuple[str, ...]] = ("default", "publish")
TERMINAL: Final = ("succeeded", "partial", "failed", "cancelled", "dead_letter")
FAILED: Final = ("failed", "dead_letter")


@asynccontextmanager
async def rollup_session() -> AsyncIterator[AsyncSession]:
    async with platform_scope() as session:
        yield session


def aware_utc(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def _utc_day(column: ColumnElement[datetime]) -> ColumnElement[date]:
    return cast(func.timezone("UTC", column), Date)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _money(value: object) -> Decimal:
    return Decimal(str(value) if value is not None else "0").quantize(MONEY)


# ---- cost ----


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
class TenantBudget:
    spent: Decimal
    reserved: Decimal
    cap: Decimal

    @property
    def used(self) -> Decimal:
        return self.spent + self.reserved


@dataclass(frozen=True)
class SpendToday:
    global_usd: Decimal
    per_tenant: dict[UUID, Decimal]
    global_reserved: Decimal = ZERO
    budgets: dict[UUID, TenantBudget] = field(default_factory=dict)

    @property
    def global_used(self) -> Decimal:
        return self.global_usd + self.global_reserved


@dataclass(frozen=True)
class ListingCost:
    listing_id: UUID
    tenant_id: UUID
    tenant_name: str
    cost_usd: Decimal
    runs: int


@dataclass(frozen=True)
class GroupCost:
    key: str
    cost_usd: Decimal
    runs: int


async def daily_spend(session: AsyncSession, now: datetime, days: int = 7) -> list[DaySpend]:
    if isinstance(days, bool) or not 1 <= days <= 92:
        raise ValueError("days must be in 1..92")
    today = aware_utc(now).date()
    first = today - timedelta(days=days - 1)
    start, _ = _day_bounds(first)
    day = _utc_day(AgentRun.started_at)  # type: ignore[arg-type]
    stmt = (
        select(day, Tenant.slug, func.sum(AgentRun.cost_usd))
        .join(Tenant, Tenant.id == AgentRun.tenant_id)
        .where(AgentRun.started_at >= start)
        .group_by(day, Tenant.slug)
        .order_by(day, Tenant.slug)
    )
    totals: dict[date, dict[str, Decimal]] = {first + timedelta(days=n): {} for n in range(days)}
    for spent_day, slug, total in (await session.execute(stmt)).all():
        if spent_day in totals:
            totals[spent_day][slug] = _money(total)
    return [
        DaySpend(d, sum(per.values(), ZERO).quantize(MONEY), per)
        for d, per in sorted(totals.items())
    ]


async def cost_per_published_listing(session: AsyncSession, now: datetime) -> CostPerListing:
    since = aware_utc(now) - COST_WINDOW
    spend_stmt = select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(
        AgentRun.started_at >= since
    )
    published_stmt = select(func.count()).where(Listing.first_published_at >= since)
    spend = _money((await session.execute(spend_stmt)).scalar_one())
    published = int((await session.execute(published_stmt)).scalar_one())
    per = None if published == 0 else (spend / published).quantize(MONEY)
    return CostPerListing(spend, published, per)


async def spend_today(
    session: AsyncSession, now: datetime, *, default_cap: Decimal | None = None
) -> SpendToday:
    """Today's (UTC day of `started_at`) spend and `listing_generate` reservations per tenant, and
    each such tenant's cap: its `llm_daily_budget_usd`, else `default_cap` (default: the
    `LLM_TENANT_DAILY_BUDGET_USD` setting). The kill switch's sums (`tenant_state`, `global_state`).
    """
    if default_cap is None:
        default_cap = Decimal(str(get_settings().llm_tenant_daily_budget_usd))
    start, end = _day_bounds(aware_utc(now).date())
    spent_stmt = (
        select(AgentRun.tenant_id, func.sum(AgentRun.cost_usd))
        .where(AgentRun.started_at >= start, AgentRun.started_at < end)
        .group_by(AgentRun.tenant_id)
    )
    reserved_stmt = (
        select(JobRun.tenant_id, func.sum(JobRun.cost_reserved_usd))
        .where(
            JobRun.kind == JobKind.LISTING_GENERATE.value,
            JobRun.started_at >= start,
            JobRun.started_at < end,
        )
        .group_by(JobRun.tenant_id)
    )
    per = {tenant: _money(total) for tenant, total in (await session.execute(spent_stmt)).all()}
    reserved = {
        tenant: _money(total) for tenant, total in (await session.execute(reserved_stmt)).all()
    }
    active = set(per) | set(reserved)
    caps: dict[UUID, Decimal] = {}
    if active:
        cap_stmt = select(Tenant.id, Tenant.llm_daily_budget_usd).where(Tenant.id.in_(active))
        caps = {
            tenant: default_cap if own is None else Decimal(own)
            for tenant, own in (await session.execute(cap_stmt)).all()
        }
    budgets = {
        tenant: TenantBudget(
            per.get(tenant, ZERO), reserved.get(tenant, ZERO), caps.get(tenant, default_cap)
        )
        for tenant in active
    }
    return SpendToday(
        sum(per.values(), ZERO).quantize(MONEY),
        per,
        sum(reserved.values(), ZERO).quantize(MONEY),
        budgets,
    )


def _listing_costs(since: datetime | None) -> ColumnElement[bool] | None:
    return None if since is None else AgentRun.started_at >= since


async def cost_per_listing(
    session: AsyncSession, now: datetime, *, days: int = 30, limit: int = TOP_LISTINGS
) -> list[ListingCost]:
    if isinstance(limit, bool) or not 1 <= limit <= TOP_LISTINGS:
        raise ValueError(f"limit must be in 1..{TOP_LISTINGS}")
    since = aware_utc(now) - timedelta(days=days)
    cost = func.sum(AgentRun.cost_usd)
    stmt = (
        select(AgentRun.listing_id, AgentRun.tenant_id, Tenant.name, cost, func.count())
        .join(Tenant, Tenant.id == AgentRun.tenant_id)
        .where(AgentRun.listing_id.is_not(None), AgentRun.started_at >= since)
        .group_by(AgentRun.listing_id, AgentRun.tenant_id, Tenant.name)
        .order_by(cost.desc(), AgentRun.listing_id)
        .limit(limit)
    )
    return [
        ListingCost(listing, tenant, name, _money(total), int(runs))
        for listing, tenant, name, total, runs in (await session.execute(stmt)).all()
    ]


async def listing_cost(session: AsyncSession, listing_id: UUID) -> ListingCost | None:
    """One listing's all-time AI cost (`sum(agent_runs.cost_usd)` of its runs)."""
    found = (
        await session.execute(
            select(Listing.tenant_id, Tenant.name)
            .join(Tenant, Tenant.id == Listing.tenant_id)
            .where(Listing.id == listing_id)
        )
    ).one_or_none()
    if found is None:
        return None
    tenant, name = found
    total, runs = (
        await session.execute(
            select(func.coalesce(func.sum(AgentRun.cost_usd), 0), func.count()).where(
                AgentRun.tenant_id == tenant, AgentRun.listing_id == listing_id
            )
        )
    ).one()
    return ListingCost(listing_id, tenant, name, _money(total), int(runs))


async def _by(
    session: AsyncSession, column: ColumnElement[str | None], day: date
) -> list[GroupCost]:
    start, end = _day_bounds(day)
    cost = func.sum(AgentRun.cost_usd)
    key = func.coalesce(column, "unknown")
    stmt = (
        select(key, cost, func.count())
        .where(AgentRun.started_at >= start, AgentRun.started_at < end)
        .group_by(key)
        .order_by(cost.desc(), key)
    )
    return [GroupCost(str(k), _money(c), int(n)) for k, c, n in (await session.execute(stmt)).all()]


async def cost_by_model(session: AsyncSession, day: date) -> list[GroupCost]:
    return await _by(session, AgentRun.model_used, day)  # type: ignore[arg-type]


async def cost_by_trigger(session: AsyncSession, day: date) -> list[GroupCost]:
    return await _by(session, AgentRun.trigger, day)  # type: ignore[arg-type]


# ---- jobs ----


@dataclass(frozen=True)
class KindOutcomes:
    kind: str
    terminal: int
    failed: int


@dataclass(frozen=True)
class QueueAge:
    queue: str
    oldest_s: int | None


@dataclass(frozen=True)
class KindTiming:
    kind: str
    n: int
    avg_s: float
    max_s: float


async def job_outcomes(
    session: AsyncSession, now: datetime, window: timedelta = JOB_WINDOW
) -> list[KindOutcomes]:
    """Per job kind, the jobs that reached a terminal status in `[now - window, now]`."""
    current = aware_utc(now)
    stmt = (
        select(
            JobRun.kind,
            func.count(),
            func.count().filter(JobRun.status.in_(FAILED)),
        )
        .where(
            JobRun.status.in_(TERMINAL),
            JobRun.finished_at >= current - window,
            JobRun.finished_at <= current,
        )
        .group_by(JobRun.kind)
        .order_by(JobRun.kind)
    )
    return [KindOutcomes(k, int(t), int(f)) for k, t, f in (await session.execute(stmt)).all()]


async def oldest_queued(session: AsyncSession, now: datetime) -> list[QueueAge]:
    """Per queue (every known queue listed), the age of its oldest `queued` job."""
    current = aware_utc(now)
    stmt = (
        select(JobRun.queue, func.min(JobRun.queued_at))
        .where(JobRun.status == "queued")
        .group_by(JobRun.queue)
    )
    oldest: dict[str, datetime] = {str(q): at for q, at in (await session.execute(stmt)).all()}
    out: list[QueueAge] = []
    for queue in sorted(set(QUEUES) | set(oldest)):
        at = oldest.get(queue)
        out.append(
            QueueAge(queue, None if at is None else max(0, int((current - at).total_seconds())))
        )
    return out


async def processing_time(
    session: AsyncSession, now: datetime, window: timedelta = JOB_WINDOW
) -> list[KindTiming]:
    """Per job kind, `finished_at - started_at` of the jobs finished in the window that started
    (a job cancelled while queued never ran and is left out)."""
    current = aware_utc(now)
    seconds = func.extract("epoch", JobRun.finished_at - JobRun.started_at)
    stmt = (
        select(JobRun.kind, func.count(), func.avg(seconds), func.max(seconds))
        .where(
            JobRun.started_at.is_not(None),
            JobRun.finished_at >= current - window,
            JobRun.finished_at <= current,
        )
        .group_by(JobRun.kind)
        .order_by(JobRun.kind)
    )
    return [
        KindTiming(k, int(n), round(float(avg), 3), round(float(mx), 3))
        for k, n, avg, mx in (await session.execute(stmt)).all()
    ]
