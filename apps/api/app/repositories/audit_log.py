"""Audit reads (S19 T3; DATABASE.md §5.2 audit_log): a listing's history, newest first.

Writes go through `AuditRepository.write` (the database's `audit()` function); this repository
only reads, through the tenant-scoped select, so another tenant's rows never come back.
"""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import literal, tuple_

from app.db.models.audit_brand import AuditLog
from app.repositories.base import TenantScopedRepository

PAGE_LIMIT_MAX: Final = 200


class AuditLogRepository(TenantScopedRepository[AuditLog]):
    model = AuditLog

    async def page_for_entity(
        self,
        entity_type: str,
        entity_id: uuid.UUID,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> tuple[builtins.list[AuditLog], bool]:
        """One keyset page over `(occurred_at, id)` DESC and whether more rows follow."""
        if isinstance(limit, bool) or not 1 <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be in 1..{PAGE_LIMIT_MAX}")
        stmt = self._select().where(
            AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id
        )
        if after is not None:
            at, row_id = after
            bound = tuple_(
                literal(at, AuditLog.occurred_at.type), literal(row_id, AuditLog.id.type)
            )
            stmt = stmt.where(tuple_(AuditLog.occurred_at, AuditLog.id) < bound)
        stmt = stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit + 1)
        rows = builtins.list((await self.db.execute(stmt)).scalars())
        return rows[:limit], len(rows) > limit

    async def page_for_listing(
        self,
        listing_id: uuid.UUID,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> tuple[builtins.list[AuditLog], bool]:
        return await self.page_for_entity("listing", listing_id, after=after, limit=limit)
