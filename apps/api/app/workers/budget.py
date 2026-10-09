"""The budget reservation a generation job takes at claim (DATABASE.md:997-998; S18 T7, D10, CP9).

`reserve(tenant_id, job_id, attempt)` runs one tenant transaction A:
1. `pg_advisory_xact_lock(hashtext('budget:' || tenant_id))`, then the `budget:global` lock;
   always in this order, so two claims never deadlock;
2. the tenant's spend plus reservations of today (UTC) against its cap;
3. every tenant's spend plus reservations against `LLM_DAILY_BUDGET_USD`, read in a separate
   read-only `platform_scope()` session B opened and closed while A holds both locks (B cannot
   share A, CANON-ADDENDUM.md:181); each earlier claim committed before it released the global
   lock, so B sees it;
4. `cost_reserved_usd = GENERATION_RESERVATION_USD` on the claimed attempt (fenced);
5. the commit, which releases both locks.

A reservation that would take either sum past its cap raises `BudgetRefused(scope)`. A timeout or
a crash leaves the reservation counted (DATABASE.md:998); the terminal write zeroes it only when
the job's own `agent_runs` replace it (CP10).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from app.core.logging import get_logger
from app.db.session import platform_scope, unit_of_work
from app.repositories.budget import BudgetRepository, global_state
from app.repositories.jobs import JobRunRepository
from app.services.budget import platform_cap, tenant_default_cap

log = get_logger(__name__)

# "not specified" in the design; tuned against real model prices at S24 (D10).
GENERATION_RESERVATION_USD: Final = Decimal("0.10")


class BudgetRefused(Exception):  # noqa: N818 - the name the plan uses
    def __init__(self, scope: Literal["tenant", "global"]) -> None:
        super().__init__(f"budget refused ({scope})")
        self.scope = scope


async def reserve(
    tenant_id: UUID,
    job_id: UUID,
    attempt: int,
    *,
    now: datetime | None = None,
    amount: Decimal = GENERATION_RESERVATION_USD,
) -> bool:
    """Reserve `amount` for the claimed attempt; False when the fence no longer holds."""
    day = (now or datetime.now(UTC)).astimezone(UTC).date()
    async with unit_of_work(tenant_id) as session:
        budget = BudgetRepository(session, tenant_id)
        await budget.lock_tenant()
        await budget.lock_global()
        own = await budget.tenant_state(day, default_cap=tenant_default_cap())
        if own.used + amount > own.cap:
            log.info("budget.refused", scope="tenant", job_id=str(job_id), tenant_id=str(tenant_id))
            raise BudgetRefused("tenant")
        async with platform_scope() as platform:
            everyone = await global_state(platform, day, cap=platform_cap())
        if everyone.used + amount > everyone.cap:
            log.info("budget.refused", scope="global", job_id=str(job_id), tenant_id=str(tenant_id))
            raise BudgetRefused("global")
        return await JobRunRepository(session, tenant_id).reserve_cost(job_id, attempt, amount)
