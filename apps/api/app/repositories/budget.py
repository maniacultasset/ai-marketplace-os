"""The AI budget reads and locks (DATABASE.md:993-998; ENV.md:90-91; S18 T6, T7, D10).

A budget day is the UTC day of `started_at`. A tenant has spent `SUM(agent_runs.cost_usd)` that
day and holds `SUM(job_runs.cost_reserved_usd)` over its `listing_generate` jobs claimed that day;
its cap is `tenants.llm_daily_budget_usd`, else the `LLM_TENANT_DAILY_BUDGET_USD` default the
caller passes. `global_state(platform_session, day)` is the same sum over every tenant, read in the
read-only platform session (the caller owns it; CANON-ADDENDUM.md:181).

`lock_tenant()` and `lock_global()` take `pg_advisory_xact_lock(hashtext('budget:' || ...))` in the
caller's tenant transaction: always the tenant first, then the global one (one order, so two
claims never deadlock).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

from mkos_schemas.enums import JobKind
from sqlalchemy import func, select

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.ops import JobRun
from app.db.models.runs import AgentRun
from app.db.models.tenancy import Tenant
from app.repositories.base import TenantScopedRepository

GLOBAL_LOCK_KEY: Final = "budget:global"
ZERO: Final = Decimal("0")


@dataclass(frozen=True, slots=True)
class BudgetState:
    spent: Decimal
    reserved: Decimal
    cap: Decimal

    @property
    def used(self) -> Decimal:
        return self.spent + self.reserved


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


class _Jobs(TenantScopedRepository[JobRun]):
    model = JobRun


class _Tenants(TenantScopedRepository[Tenant]):
    model = Tenant


class BudgetRepository(TenantScopedRepository[AgentRun]):
    model = AgentRun

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("budget lock for another tenant's unit of work")

    async def tenant_state(self, day: date, *, default_cap: Decimal) -> BudgetState:
        start, end = day_bounds(day)
        spent = (
            self._select()
            .where(AgentRun.started_at >= start, AgentRun.started_at < end)
            .with_only_columns(func.coalesce(func.sum(AgentRun.cost_usd), ZERO))
        )
        reserved = (
            _Jobs(self.db, self.tenant_id)
            ._select()
            .where(
                JobRun.kind == JobKind.LISTING_GENERATE.value,
                JobRun.started_at >= start,
                JobRun.started_at < end,
            )
            .with_only_columns(func.coalesce(func.sum(JobRun.cost_reserved_usd), ZERO))
        )
        cap = (
            _Tenants(self.db, self.tenant_id)
            ._select()
            .with_only_columns(Tenant.llm_daily_budget_usd)
        )
        spent_value = Decimal((await self.db.execute(spent)).scalar_one())
        reserved_value = Decimal((await self.db.execute(reserved)).scalar_one())
        own_cap = (await self.db.execute(cap)).scalar_one_or_none()
        return BudgetState(
            spent=spent_value,
            reserved=reserved_value,
            cap=default_cap if own_cap is None else Decimal(own_cap),
        )

    async def lock_tenant(self) -> None:
        self._own_session()
        await self.db.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(f"budget:{self.tenant_id}")))
        )

    async def lock_global(self) -> None:
        self._own_session()
        await self.db.execute(select(func.pg_advisory_xact_lock(func.hashtext(GLOBAL_LOCK_KEY))))


async def global_state(platform_session: Any, day: date, *, cap: Decimal) -> BudgetState:
    """Every tenant's spend and reservations of `day`. Cross-tenant: run it only in the read-only
    `platform_scope()` session (`app/workers/budget.py`)."""
    start, end = day_bounds(day)
    spent = select(func.coalesce(func.sum(AgentRun.cost_usd), ZERO)).where(
        AgentRun.started_at >= start, AgentRun.started_at < end
    )
    reserved = select(func.coalesce(func.sum(JobRun.cost_reserved_usd), ZERO)).where(
        JobRun.kind == JobKind.LISTING_GENERATE.value,
        JobRun.started_at >= start,
        JobRun.started_at < end,
    )
    return BudgetState(
        spent=Decimal((await platform_session.execute(spent)).scalar_one()),
        reserved=Decimal((await platform_session.execute(reserved)).scalar_one()),
        cap=cap,
    )
