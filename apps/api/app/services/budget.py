"""The tenant AI budget pre-check at the API (DW8, AC 14; DATABASE.md:997; ENV.md:91; S18 T6).

`precheck(uow, now)` refuses a fresh generate or regenerate when the tenant's spend plus its live
reservations for the UTC day of `now` has reached its cap: `BudgetExceededError`, which the route
renders as 429 `BUDGET_EXCEEDED` with `Retry-After` = whole seconds to the next UTC midnight
(at least 1). Nothing is written and nothing is enqueued. The body states no amount. The global
platform cap is checked at claim only (`app/workers/budget.py`, the D10 departure).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Final

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.config import get_settings
from app.core.errors import DomainError
from app.repositories.budget import BudgetState
from app.repositories.uow import TenantUnitOfWork

BUDGET_MESSAGE: Final = (
    "The daily AI budget for this brand is used up; try again after midnight UTC."
)


class BudgetExceededError(DomainError):
    """The tenant is at its daily AI cap. `retry_after_s` is the `Retry-After` value."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__("BUDGET_EXCEEDED")
        self.retry_after_s = retry_after_s


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
    state = await tenant_state(uow, now)
    if state.used >= state.cap:
        raise BudgetExceededError(seconds_to_utc_midnight(now))
