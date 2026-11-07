"""`AiCreditRepository`: the one `ai_credit_state` row (revision 0021, SI10; DATABASE.md §5.4).

The table has no `tenant_id` (a platform row, readable by app_rw in any session), so this is not
a `TenantScopedRepository`. Recording a check and setting a pause are plain app_rw UPDATEs;
clearing a pause is guarded in the database: `auto_clear` runs the migrator-owned
`app_ai_credit_auto_clear` (CP-D3), and `resume` passes the guard only when the session carries
a platform admin's `app.user_id`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar, cast

from sqlalchemy import Numeric, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.platform import AiCreditState
from app.services.ai_credit import CreditRow, PauseReason

ROW_ID = 1


class AiCreditRepository:
    model: ClassVar[type[AiCreditState]] = AiCreditState

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def read(self) -> CreditRow:
        t = AiCreditState
        found = (
            await self.db.execute(
                select(
                    t.paused_reason,
                    t.paused_at,
                    t.limit_usd,
                    t.usage_usd,
                    t.remaining_usd,
                    t.checked_at,
                    t.resumed_at,
                    t.resumed_by_user_id,
                ).where(t.id == ROW_ID)
            )
        ).one_or_none()
        if found is None:  # the migration seeds row 1; a missing row reads as never checked
            return CreditRow()
        return CreditRow(
            paused_reason=cast("PauseReason | None", found.paused_reason),
            paused_at=found.paused_at,
            limit_usd=found.limit_usd,
            usage_usd=found.usage_usd,
            remaining_usd=found.remaining_usd,
            checked_at=found.checked_at,
            resumed_at=found.resumed_at,
            resumed_by_user_id=found.resumed_by_user_id,
        )

    async def record_check(
        self,
        *,
        limit: Decimal | None,
        usage: Decimal | None,
        remaining: Decimal | None,
        at: datetime,
    ) -> None:
        await self.db.execute(
            update(AiCreditState)
            .where(AiCreditState.id == ROW_ID)
            .values(limit_usd=limit, usage_usd=usage, remaining_usd=remaining, checked_at=at)
        )

    async def pause(self, reason: PauseReason, at: datetime) -> bool:
        """Set the pause unless one is set already (first wins); True when this call set it."""
        result = await self.db.execute(
            update(AiCreditState)
            .where(AiCreditState.id == ROW_ID, AiCreditState.paused_at.is_(None))
            .values(paused_reason=reason, paused_at=at)
            .returning(AiCreditState.id)
        )
        return result.first() is not None

    async def auto_clear(self, remaining: Decimal | None) -> bool:
        """The CP-D3 clear against the stored previous check (run it before `record_check`)."""
        cleared = await self.db.scalar(
            select(func.app_ai_credit_auto_clear(literal(remaining, Numeric(12, 6))))
        )
        return bool(cleared)

    async def resume(self, at: datetime) -> None:
        """Staff Resume: clears the pause and records the acting admin (`app.user_id`). The
        database guard refuses it (GRD01) for any other session. Repeatable."""
        await self.db.execute(
            update(AiCreditState)
            .where(AiCreditState.id == ROW_ID)
            .values(
                paused_reason=None,
                paused_at=None,
                resumed_at=at,
                resumed_by_user_id=func.app_user_id(),
            )
        )


__all__ = ["ROW_ID", "AiCreditRepository"]
