"""The AI credit rules (SI10): one pure module, so the API precheck, the worker claim and the
admin read cannot disagree (PLAN D4). Imports only the standard library, `mkos_schemas` and
`app.core`, and the row shape from `app.repositories.ai_credit` (repositories never import
services).

- `admission_refusal` is the one admission predicate, in rule order: 1 the credit (a pause, or a
  fresh `/key` check below the floor), 2 the tenant's daily cap, 3 the platform's daily cap. Rules
  2 and 3 are `used + amount > cap`.
- `classify_refusal` reads a finished run: a credit refusal (a query-embed 402, or a failed record
  with `AI_CREDIT_EXHAUSTED`, or the legacy `BUDGET_EXCEEDED` of pipeline 0.3.1, decision 16)
  beats a key suspicion (an embed 401/403, or a 0.3.2 `AI_PROVIDER_KEY_INVALID` record). Only a
  401/403 from `/key` itself proves a bad key (D5, CP-D15).
- The clear rule is not here: it lives only in the SQL function `app_ai_credit_auto_clear`
  (revision 0021, CP-D17), whose floor equals `CREDIT_FLOOR_USD`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from mkos_schemas.codes import ErrorCode

from app.repositories.ai_credit import CreditRow, PauseReason

if TYPE_CHECKING:
    from mkos_schemas.agent import AgentRunRecord

    from app.repositories.budget import BudgetState

# Code constants beside GENERATION_RESERVATION_USD (P:§7 decision 5): no env vars.
CREDIT_FLOOR_USD: Final = Decimal("0.50")
FRESH_FOR: Final = timedelta(minutes=15)
CREDIT_ALERT_PCTS: Final = (50, 80, 95)

Refusal = Literal["credit_exhausted", "key_invalid", "tenant", "global"]
EmbedRefusal = Literal["credit", "key_suspect"]
CreditState = Literal["ok", "low", "paused", "unknown"]

REASON_CODE: Final[Mapping[PauseReason, ErrorCode]] = MappingProxyType(
    {
        "credit_exhausted": ErrorCode.AI_CREDIT_EXHAUSTED,
        "key_invalid": ErrorCode.AI_PROVIDER_KEY_INVALID,
    }
)
CREDIT_CODES: Final = frozenset({ErrorCode.AI_CREDIT_EXHAUSTED, ErrorCode.AI_PROVIDER_KEY_INVALID})
# A failed pipeline record that means "out of credit": 0.3.2's code, and 0.3.1's 402 mapping
# (removed in the 0.3.2 pin PR, decision 16).
_CREDIT_RECORD_CODES: Final = frozenset({ErrorCode.AI_CREDIT_EXHAUSTED, ErrorCode.BUDGET_EXCEEDED})


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now


def _fresh(row: CreditRow, now: datetime) -> bool:
    return row.checked_at is not None and now - row.checked_at <= FRESH_FOR


def _fresh_low(row: CreditRow, now: datetime) -> bool:
    return (
        _fresh(row, now) and row.remaining_usd is not None and row.remaining_usd < CREDIT_FLOOR_USD
    )


def credit_refusal(row: CreditRow, now: datetime) -> PauseReason | None:
    """Rule 1: the pause reason, else `credit_exhausted` for a fresh check below the floor. A
    stale, never-checked or unlimited row admits (fail open, P:§7 decision 8)."""
    _aware(now)
    if row.paused_reason is not None:
        return row.paused_reason
    if _fresh_low(row, now):
        return "credit_exhausted"
    return None


def admission_refusal(
    row: CreditRow,
    own: BudgetState,
    everyone: BudgetState | None,
    *,
    amount: Decimal,
    now: datetime,
) -> Refusal | None:
    """The first refusal in rule order, or None. `everyone=None` means the platform sum is not
    read yet (`reserve()` reads it under its locks and calls again)."""
    reason = credit_refusal(row, now)
    if reason is not None:
        return reason
    if own.used + amount > own.cap:
        return "tenant"
    if everyone is not None and everyone.used + amount > everyone.cap:
        return "global"
    return None


def classify_refusal(
    records: Iterable[AgentRunRecord], embed: EmbedRefusal | None
) -> EmbedRefusal | None:
    """What a finished run's provider refusals mean (§2; credit beats a suspicion)."""
    suspect = embed == "key_suspect"
    if embed == "credit":
        return "credit"
    for record in records:
        if record.status != "failed" or record.error_report is None:
            continue
        code = record.error_report.error_code
        if code in _CREDIT_RECORD_CODES:
            return "credit"
        if code == ErrorCode.AI_PROVIDER_KEY_INVALID:
            suspect = True
    return "key_suspect" if suspect else None


def state_of(row: CreditRow, now: datetime) -> CreditState:
    """The admin view (CP-D9): paused, else low (admission refuses), else unknown (never checked
    or stale), else ok."""
    _aware(now)
    if row.paused_reason is not None:
        return "paused"
    if _fresh_low(row, now):
        return "low"
    if not _fresh(row, now):
        return "unknown"
    return "ok"


__all__ = [
    "CREDIT_ALERT_PCTS",
    "CREDIT_CODES",
    "CREDIT_FLOOR_USD",
    "FRESH_FOR",
    "REASON_CODE",
    "CreditRow",
    "CreditState",
    "EmbedRefusal",
    "PauseReason",
    "Refusal",
    "admission_refusal",
    "classify_refusal",
    "credit_refusal",
    "state_of",
]
