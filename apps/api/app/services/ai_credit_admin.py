"""The platform admin's AI credit read and Resume (SI10 T13; PLAN D1, AC15).

`view(now)` reads the credit row and today's platform aggregate (no tenant rows) through
`workers.budget.platform_view`. `resume(principal, now)` clears the pause in a session that
carries the admin's `app.user_id`, so the database guard (revision 0021) checks the admin itself;
it is repeatable and logs `ai_credit.resumed` with the user id only. The floor still applies at
the next check (P:§7 decision 9)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.ai_credit import AiCreditRepository, CreditRow
from app.repositories.transactions import identity_session, set_gucs
from app.services.ai_credit import CREDIT_FLOOR_USD, CreditState, state_of

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AiCreditView:
    state: CreditState
    credit: CreditRow
    floor_usd: Decimal
    today_spend_usd: Decimal
    daily_cap_usd: Decimal


async def view(now: datetime | None = None) -> AiCreditView:
    from app.workers.budget import platform_view

    at = now or datetime.now(UTC)
    found = await platform_view(at.astimezone(UTC).date())
    return AiCreditView(
        state=state_of(found.credit, at),
        credit=found.credit,
        floor_usd=CREDIT_FLOOR_USD,
        today_spend_usd=found.everyone.used,
        daily_cap_usd=found.everyone.cap,
    )


async def resume(principal: Principal, now: datetime | None = None) -> AiCreditView:
    at = now or datetime.now(UTC)
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        await AiCreditRepository(session).resume(at)
    log.info("ai_credit.resumed", user_id=str(principal.user_id))
    return await view(at)
