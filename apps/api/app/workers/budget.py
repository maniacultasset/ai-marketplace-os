"""The budget reservation a generation job takes at claim (DATABASE.md:997-998; S18 T7, D10, CP9),
and the AI credit admission and pause (SI10 D4, D5).

`reserve(tenant_id, job_id, attempt)` runs one tenant transaction A:
0. the AI credit row (`ai_credit_state`, revision 0021): a pause, or a fresh `/key` check below
   the floor, refuses at once as `BudgetRefused("credit", reason)`, before any lock or write;
1. `pg_advisory_xact_lock(hashtext('budget:' || tenant_id))`, then the `budget:global` lock;
   always in this order, so two claims never deadlock;
2. the tenant's spend plus reservations of today (UTC) plus `amount` against its cap;
3. every tenant's spend plus reservations plus `amount` against `LLM_DAILY_BUDGET_USD`, read in a
   separate read-only `platform_scope()` session B opened and closed while A holds both locks (B
   cannot share A, CANON-ADDENDUM.md:181); each earlier claim committed before it released the
   global lock, so B sees it;
4. `cost_reserved_usd = GENERATION_RESERVATION_USD` on the claimed attempt (fenced);
5. the commit, which releases both locks.

Rules 0-3 are `app.services.ai_credit.admission_refusal`, the same predicate `admission()` runs
for the API precheck and for any other AI spend path before it enqueues (D4), so the two cannot
disagree.

A `brand_embed` job reserves the same way before each paid embedding call, with `amount` the
call's upper-bound cost (`app.services.brand_embed.reservation_usd`), and `release` settles it to
zero once the call's `agent_runs` row holds the real spend.

Every terminal path zeroes the reservation (DATABASE.md `agent_runs` "Budget reservation", SI10):
the job's own `agent_runs` replace it, or the job failed, timed out or was refused. A crash before
the terminal write keeps it until the reaper settles the row.

`pause_ai(reason)` sets the platform pause (first wins) and raises one error-level
`llm_provider_refused` alert; `confirm_key_refusal()` asks OpenRouter's `/key` whether an
upstream 401/403 means the key itself is bad (one call per process per 60 s, shared).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

import httpx

from app.adapters.openrouter.key import KeyRefused, KeyUnavailable, fetch_key
from app.core.alerts import floor_window
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.model_prices import is_usable_openai_key
from app.db.session import platform_scope, unit_of_work
from app.repositories.ai_credit import AiCreditRepository
from app.repositories.budget import BudgetRepository, BudgetState, global_state, image_edit_spend
from app.repositories.image_candidates import ImageCandidateRepository
from app.repositories.jobs import JobRunRepository
from app.repositories.transactions import identity_session
from app.services.ai_credit import (
    REASON_CODE,
    CreditRow,
    PauseReason,
    Refusal,
    admission_refusal,
    credit_refusal,
)
from app.services.budget import platform_cap, tenant_default_cap
from app.workers.alerting import alert

log = get_logger(__name__)

# About 10x the real cost of one generation (USD 0.002, V1-RELEASE-CHECKLIST.md:5-7; SI10).
GENERATION_RESERVATION_USD: Final = Decimal("0.02")
KEY_CONFIRM_FOR: Final = timedelta(seconds=60)
PAUSE_ALERT_WINDOW: Final = timedelta(hours=1)

# SI7 (CP-D6; live check D17, 2026-11-07): image output tokens are not bounded by `max_tokens`
# (1290 to 2582 seen with google/gemini-2.5-flash-image), so one edit cost USD 0.039 to 0.078;
# the reservation covers the top of that range.
IMAGE_EDIT_RESERVATION_USD: Final = Decimal("0.10")
ImageScope = Literal["image_tenant", "image_global"]
Scope = Literal["tenant", "global", "credit", "image_tenant", "image_global"]


class BudgetRefused(Exception):  # noqa: N818 - the name the plan uses
    def __init__(self, scope: Scope, reason: PauseReason | None = None) -> None:
        super().__init__(f"budget refused ({scope})")
        self.scope = scope
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PlatformView:
    """The admin read (SI10 T13): the credit row and the platform's aggregate day, no tenant."""

    credit: CreditRow
    everyone: BudgetState


def _day(now: datetime) -> date:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC).date()


async def admission(
    tenant_id: UUID, now: datetime, *, amount: Decimal = GENERATION_RESERVATION_USD
) -> Refusal | None:
    """The one admission read (D4): the credit row and the tenant's day in a short tenant read,
    then the platform sum only when those pass. Nothing is written."""
    async with unit_of_work(tenant_id) as session:
        credit, own = await _admission_reads(session, tenant_id, _day(now))
    return await _admission_rest(credit, own, now, amount)


async def admission_on(
    session: Any,
    tenant_id: UUID,
    now: datetime,
    *,
    amount: Decimal = GENERATION_RESERVATION_USD,
) -> Refusal | None:
    """`admission` for a caller already inside a tenant transaction (SI11: the resolution
    executor in a terminal hook): the credit row and the tenant's day read on `session`, the
    platform sum in a separate read-only `platform_scope()` session only when those pass (as
    `reserve` reads it while it holds the tenant transaction). Nothing is written."""
    credit, own = await _admission_reads(session, tenant_id, _day(now))
    return await _admission_rest(credit, own, now, amount)


async def _admission_reads(
    session: Any, tenant_id: UUID, day: date
) -> tuple[CreditRow, BudgetState]:
    credit = await AiCreditRepository(session).read()
    own = await BudgetRepository(session, tenant_id).tenant_state(
        day, default_cap=tenant_default_cap()
    )
    return credit, own


async def _admission_rest(
    credit: CreditRow, own: BudgetState, now: datetime, amount: Decimal
) -> Refusal | None:
    refusal = admission_refusal(credit, own, None, amount=amount, now=now)
    if refusal is not None:
        return refusal
    async with platform_scope() as platform:
        everyone = await global_state(platform, _day(now), cap=platform_cap())
    return admission_refusal(credit, own, everyone, amount=amount, now=now)


async def platform_view(day: date) -> PlatformView:
    async with platform_scope() as platform:
        credit = await AiCreditRepository(platform).read()
        everyone = await global_state(platform, day, cap=platform_cap())
    return PlatformView(credit=credit, everyone=everyone)


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


async def platform_image_spend(day: date) -> Decimal:
    """Today's image-edit spend of the whole platform, in its own short read-only session."""
    async with platform_scope() as platform:
        return await image_edit_spend(platform, day)


async def image_edit_refusal(
    session: Any,
    tenant_id: UUID,
    day: date,
    limits: ImageEditLimits,
    amount: Decimal,
    *,
    platform_spent: Decimal | None = None,
) -> ImageScope | None:
    """The D21 checks, in the caller's tenant transaction under the budget locks: the tenant's
    count of today's AI candidates, then the platform's image spend plus `amount` against its
    share of the platform cap. The worker's claim reads that spend in a separate read-only
    platform session while it holds the locks; the API passes `platform_spent`, read before
    its transaction, so a request never holds two pooled connections (the claim re-checks)."""
    count = await ImageCandidateRepository(session, tenant_id).count_ai_today(
        day, exclude_id=limits.exclude_id
    )
    if count >= limits.tenant_daily_limit:
        return "image_tenant"
    spent = platform_spent if platform_spent is not None else await platform_image_spend(day)
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
    at = now or datetime.now(UTC)
    day = _day(at)
    async with unit_of_work(tenant_id) as session:
        credit = await AiCreditRepository(session).read()
        reason = credit_refusal(credit, at)
        if reason is not None:
            log.info(
                "budget.refused",
                scope="credit",
                reason=reason,
                job_id=str(job_id),
                tenant_id=str(tenant_id),
            )
            raise BudgetRefused("credit", reason)
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
        if admission_refusal(credit, own, None, amount=amount, now=at) == "tenant":
            log.info("budget.refused", scope="tenant", job_id=str(job_id), tenant_id=str(tenant_id))
            raise BudgetRefused("tenant")
        async with platform_scope() as platform:
            everyone = await global_state(platform, day, cap=platform_cap())
        if admission_refusal(credit, own, everyone, amount=amount, now=at) == "global":
            log.info("budget.refused", scope="global", job_id=str(job_id), tenant_id=str(tenant_id))
            raise BudgetRefused("global")
        return await JobRunRepository(session, tenant_id).reserve_cost(job_id, attempt, amount)


async def release(tenant_id: UUID, job_id: UUID, attempt: int) -> bool:
    """Settle the claimed attempt's reservation to zero (its `agent_runs` now hold the spend);
    False when the fence no longer holds."""
    async with unit_of_work(tenant_id) as session:
        return await JobRunRepository(session, tenant_id).reserve_cost(job_id, attempt, Decimal(0))


async def pause_ai(
    reason: PauseReason, *, job_id: UUID | None = None, now: datetime | None = None
) -> bool:
    """Pause AI for every tenant (first wins) and, when this call set it, raise one error-level
    `llm_provider_refused` alert after the commit. A failure to write is logged at ERROR and
    swallowed (P:§2 D5): the caller's job outcome stands either way. True when this call set it."""
    at = now or datetime.now(UTC)
    try:
        async with identity_session() as session:
            changed = await AiCreditRepository(session).pause(reason, at)
    except Exception as exc:
        log.error("ai_credit.pause_failed", reason=reason, error_class=type(exc).__name__)
        return False
    if not changed:
        return False
    log.warning("ai_credit.paused", reason=reason)
    fields: dict[str, Any] = {"error_code": REASON_CODE[reason].value}
    if job_id is not None:
        fields["job_id"] = job_id
    await alert(
        "llm_provider_refused",
        key=reason,
        window_start=floor_window(at, PAUSE_ALERT_WINDOW),
        now=at,
        level="error",
        **fields,
    )
    return True


# One `/key` confirmation per process per KEY_CONFIRM_FOR; concurrent callers share it.
_key_answer: tuple[datetime, bool] | None = None
_key_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def _confirm_lock() -> asyncio.Lock:
    global _key_lock
    loop = asyncio.get_running_loop()
    if _key_lock is None or _key_lock[0] is not loop:
        _key_lock = (loop, asyncio.Lock())
    return _key_lock[1]


def reset_key_confirmation() -> None:
    """Forget the cached `/key` answer (tests, and a new process)."""
    global _key_answer
    _key_answer = None


async def confirm_key_refusal(
    *, now: datetime | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    """True only when OpenRouter's `/key` answers 401/403 (D5, CP-D15): an upstream 401/403 may
    be a moderation refusal of tenant text, so only the key check proves a bad key. A 200, an
    unavailable check, another gateway or no key → False. Worker-only: the API holds no key."""
    global _key_answer
    async with _confirm_lock():
        at = now or datetime.now(UTC)
        if _key_answer is not None and timedelta(0) <= at - _key_answer[0] < KEY_CONFIRM_FOR:
            return _key_answer[1]
        settings = get_settings()
        answer = False
        if settings.llm_gateway == "openrouter" and is_usable_openai_key(
            settings.openrouter_api_key
        ):
            key = settings.openrouter_api_key or ""
            try:
                await fetch_key(key, transport=transport)
            except KeyRefused:
                answer = True
            except KeyUnavailable as exc:
                log.warning("ai_credit.key_confirm_unavailable", error_class=type(exc).__name__)
        log.info("ai_credit.key_confirmed", refused=answer)
        _key_answer = (at, answer)
        return answer
