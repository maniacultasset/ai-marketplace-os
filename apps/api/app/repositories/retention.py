"""Platform reads and calls of the periodic maintenance tasks (S13 A10-A11; DATABASE.md
:1836-1846): the tenant list the per-tenant loops walk (read through `platform_scope()`), and
`expire_trials()` (0004, SECURITY DEFINER, EXECUTE granted to app_rw)."""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import select, text

from app.db.models.tenancy import Tenant
from app.db.session import get_engine

_EXPIRE_TRIALS: Final = text("SELECT public.expire_trials()")


async def tenant_ids(session: Any) -> list[uuid.UUID]:
    """Every tenant id, in id order. Cross-tenant: run it only in `platform_scope()`."""
    return list((await session.execute(select(Tenant.id).order_by(Tenant.id))).scalars())


async def expire_trials() -> int:
    """`expire_trials()`: every `trial` past `trial_ends_at` → `suspended`; returns the count."""
    async with get_engine().begin() as connection:
        count = await connection.scalar(_EXPIRE_TRIALS)
    return int(count or 0)
