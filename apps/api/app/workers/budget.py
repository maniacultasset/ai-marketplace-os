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

A `brand_embed` job reserves the same way before each paid embedding call, with `amount` the
call's upper-bound cost (`app.services.brand_embed.reservation_usd`), and `release` settles it to
zero once the call's `agent_runs` row holds the real spend.

A reservation that would take either sum past its cap raises `BudgetRefused(scope)`. A timeout or
a crash leaves the reservation counted (DATABASE.md:998); the terminal write zeroes it when the
job's own `agent_runs` replace it (CP10), or when the job failed before any model stage
(`app/workers/generation.py` steps 2 and 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import platform_scope, unit_of_work
from app.repositories.budget import BudgetRepository, global_state, image_edit_spend
from app.repositories.image_candidates import ImageCandidateRepository
from app.repositories.jobs import JobRunRepository
from app.services.budget import platform_cap, tenant_default_cap

log = get_logger(__name__)

# "not specified" in the design; tuned against real model prices at S24 (D10).
GENERATION_RESERVATION_USD: Final = Decimal("0.10")


# SI7 (CP-D6; live check D17, 2026-11-07): image output tokens are not bounded by `max_tokens`
# (1290 to 2582 seen with google/gemini-2.5-flash-image), so one edit cost USD 0.039 to 0.078;
# the reservation covers the top of that range.
IMAGE_EDIT_RESERVATION_USD: Final = Decimal("0.10")
RefusalScope = Literal["tenant", "global", "image_tenant", "image_global"]


class BudgetRefused(Exception):  # noqa: N818 - the name the plan uses
    def __init__(self, scope: RefusalScope) -> None:
        super().__init__(f"budget refused ({scope})")
        self.scope = scope


@dataclass(frozen=True, slots=True)
class ImageEditLimits:
    """PLAN D21: at most `tenant_daily_limit` AI candidates per tenant per UTC day, and at most
    `platform_share` of `LLM_DAILY_BUDGET_USD` spent on edits by the whole platform. `exclude_id`
    is the candidate being checked (it is already queued and must not count itself)."""

    tenant_daily_limit: int
    platform_share: Decimal
    exclude_id: UUID | None = None

    @classmethod
    def from_settings(cls, settings: Settings, exclude_id: UUID | None = None) -> ImageEditLimits:
        return cls(
            tenant_daily_limit=settings.image_edit_tenant_daily_limit,
            platform_share=settings.image_edit_platform_daily_share,
            exclude_id=exclude_id,
        )


async def image_edit_refusal(
    session: Any, tenant_id: UUID, day: date, limits: ImageEditLimits, amount: Decimal
) -> RefusalScope | None:
    """The D21 checks, in the caller's tenant transaction under the budget locks: the tenant's
    count of today's AI candidates, then the platform's image spend plus `amount` against its
    share of the platform cap (read in a separate read-only platform session)."""
    count = await ImageCandidateRepository(session, tenant_id).count_ai_today(
        day, exclude_id=limits.exclude_id
    )
    if count >= limits.tenant_daily_limit:
        return "image_tenant"
    async with platform_scope() as platform:
        spent = await image_edit_spend(platform, day)
    if spent + amount > limits.platform_share * platform_cap():
        return "image_global"
    return None


async def lock_budget(session: Any, tenant_id: UUID) -> None:
    """The two budget locks in their one order (tenant, then global), held to the commit."""
    budget = BudgetRepository(session, tenant_id)
    await budget.lock_tenant()
    await budget.lock_global()


async def reserve(
    tenant_id: UUID,
    job_id: UUID,
    attempt: int,
    *,
    now: datetime | None = None,
    amount: Decimal = GENERATION_RESERVATION_USD,
    image_edit: ImageEditLimits | None = None,
) -> bool:
    """Reserve `amount` for the claimed attempt; False when the fence no longer holds. With
    `image_edit` (SI7), the D21 limits are checked first, under the same locks."""
    day = (now or datetime.now(UTC)).astimezone(UTC).date()
    async with unit_of_work(tenant_id) as session:
        budget = BudgetRepository(session, tenant_id)
        await budget.lock_tenant()
        await budget.lock_global()
        if image_edit is not None:
            refused = await image_edit_refusal(session, tenant_id, day, image_edit, amount)
            if refused is not None:
                log.info(
                    "budget.refused", scope=refused, job_id=str(job_id), tenant_id=str(tenant_id)
                )
                raise BudgetRefused(refused)
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


async def release(tenant_id: UUID, job_id: UUID, attempt: int) -> bool:
    """Settle the claimed attempt's reservation to zero (its `agent_runs` now hold the spend);
    False when the fence no longer holds."""
    async with unit_of_work(tenant_id) as session:
        return await JobRunRepository(session, tenant_id).reserve_cost(job_id, attempt, Decimal(0))
