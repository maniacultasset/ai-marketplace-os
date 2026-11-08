"""The seller reviews of product facts (SI10 T18): `before` and `after` of every user-made
`product.fact_updated` audit row since a time, every tenant, nothing else (no tenant, product or
actor column is read). Cross-tenant: run it only in `platform_scope()` (the staff CLI)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.db.models.audit_brand import AuditLog

ACTION = "product.fact_updated"


async def fact_updates_since(session: Any, since: datetime) -> list[tuple[Any, Any]]:
    stmt = select(AuditLog.before, AuditLog.after).where(
        AuditLog.action == ACTION,
        AuditLog.actor_type == "user",
        AuditLog.occurred_at >= since,
    )
    return [(row.before, row.after) for row in (await session.execute(stmt)).all()]
