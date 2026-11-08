"""The AI admission pre-check at the API (DW8, AC 14; DATABASE.md:997; ENV.md:91; S18 T6; SI10 D4).

`precheck(uow, now)` runs the worker claim's own rule (`app.workers.budget.admission`, one
predicate in `app.services.ai_credit`) before a fresh generate or regenerate changes anything:
- the AI credit is paused, or a fresh `/key` check is below the floor: `CreditPausedError`, which
  the route renders as 503 `AI_CREDIT_EXHAUSTED` or `AI_PROVIDER_KEY_INVALID`, no `Retry-After`;
- the tenant's or the platform's spend plus live reservations plus one generation's reservation
  would pass its daily cap (UTC day of `now`): `BudgetExceededError(scope)`, rendered as 429
  `BUDGET_EXCEEDED` with `Retry-After` = whole seconds to the next UTC midnight (05:30 IST; at least 1). The
  platform cap is checked here too since SI10 (decision 6), ending the D10 claim-only departure.
Nothing is written and nothing is enqueued. No body states an amount.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.config import get_settings
from app.core.errors import DomainError
from app.repositories.budget import BudgetState
from app.repositories.uow import TenantUnitOfWork
from app.services.ai_credit import REASON_CODE, PauseReason

BUDGET_MESSAGE: Final = (
    "The daily AI budget for this brand is used up. It resets at 5:30 am IST; try again then."
)
PLATFORM_BUDGET_MESSAGE: Final = (
    "Today's AI budget for the service is used up. It resets at 5:30 am IST; try again then."
)
# P:§7 decision 7: calm, no amount, no retry.
CREDIT_MESSAGES: Final[Mapping[PauseReason, str]] = MappingProxyType(
    {
        "credit_exhausted": "AI credit is used up. Our team has been told; nothing was changed.",
        "key_invalid": (
            "AI is paused while our team fixes a settings problem; nothing was changed."
        ),
    }
)


class BudgetExceededError(DomainError):
    """A daily AI cap (the tenant's or the platform's) would be passed. `retry_after_s` is the
    `Retry-After` value."""

    def __init__(self, retry_after_s: int, scope: Literal["tenant", "global"] = "tenant") -> None:
        super().__init__("BUDGET_EXCEEDED")
        self.retry_after_s = retry_after_s
        self.scope = scope

    @property
    def message(self) -> str:
        return PLATFORM_BUDGET_MESSAGE if self.scope == "global" else BUDGET_MESSAGE


class CreditPausedError(DomainError):
    """AI is paused for every tenant (or the credit is below the floor). `code` is the reason's
    error code; `message` the decision 7 text."""

    def __init__(self, reason: PauseReason) -> None:
        super().__init__(REASON_CODE[reason].value)
        self.reason = reason
        self.code = REASON_CODE[reason]
        self.message = CREDIT_MESSAGES[reason]


def seconds_to_utc_midnight(now: datetime) -> int:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = now.astimezone(UTC)
    midnight = datetime.combine(current.date() + timedelta(days=1), time.min, tzinfo=UTC)
    return max(1, int((midnight - current).total_seconds()))


def tenant_default_cap() -> Decimal:
    return Decimal(str(get_settings().llm_tenant_daily_budget_usd))


def platform_cap() -> Decimal:
    return Decimal(str(get_settings().llm_daily_budget_usd))


def budget_report() -> ErrorReport:
    """The 429's report: one `listing` item, `source=system` (codes.py meta), no amounts."""
    meta = ERROR_CODE_META[ErrorCode.BUDGET_EXCEEDED]
    item = ErrorItem(
        field="listing",
        error_code=ErrorCode.BUDGET_EXCEEDED,
        message=BUDGET_MESSAGE,
        source=ErrorSource.SYSTEM,
        retryable=False,
        retry_class=meta.retry_class,
    )
    return ErrorReport(
        status="failed", error_code=ErrorCode.BUDGET_EXCEEDED, retryable=False, errors=[item]
    )


async def tenant_state(uow: TenantUnitOfWork, now: datetime) -> BudgetState:
    return await uow.budget.tenant_state(
        now.astimezone(UTC).date(), default_cap=tenant_default_cap()
    )


async def precheck(uow: TenantUnitOfWork, now: datetime) -> None:
    """The worker claim's admission rule for the uow's tenant (CP-D18); raises on a refusal."""
    from app.workers.budget import admission

    refusal = await admission(uow.budget.tenant_id, now)
    if refusal is None:
        return
    if refusal == "tenant" or refusal == "global":
        raise BudgetExceededError(seconds_to_utc_midnight(now), refusal)
    raise CreditPausedError(refusal)
