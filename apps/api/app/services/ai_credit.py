"""The AI credit rules (SI10): one pure module, so the API precheck, the worker claim and the
admin read cannot disagree. Imports only the standard library, `mkos_schemas` and `app.core`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

PauseReason = Literal["credit_exhausted", "key_invalid"]


@dataclass(frozen=True, slots=True)
class CreditRow:
    """`ai_credit_state` row 1 (revision 0021)."""

    paused_reason: PauseReason | None = None
    paused_at: datetime | None = None
    limit_usd: Decimal | None = None
    usage_usd: Decimal | None = None
    remaining_usd: Decimal | None = None
    checked_at: datetime | None = None
    resumed_at: datetime | None = None
    resumed_by_user_id: UUID | None = None
