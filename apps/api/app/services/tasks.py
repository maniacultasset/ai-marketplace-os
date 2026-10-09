"""The human task inbox (S19 T21; DATABASE.md §5.7 :1338-1355). Read only: answering a task and
resuming from it is C2 (after v1). `evidence` (it may hold contact data) and `answer` never leave
the service."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.core.rbac import Principal
from app.repositories.uow import tenant_uow

Keyset = tuple[datetime, uuid.UUID]
_FIELDS = (
    "id", "kind", "product_id", "colourway_id", "target_path", "marketplace_account_id",
    "image_id", "question", "reason", "expected_answer", "suggestions", "status",
    "answered_by_user_id", "answered_at", "resume_from", "applied_at", "resume_batch_id",
    "cancelled_reason", "created_at", "updated_at",
)  # fmt: skip


def task_fields(row: Any) -> dict[str, Any]:
    return {name: getattr(row, name) for name in _FIELDS}


async def list_tasks(
    principal: Principal, status: str, *, after: Keyset | None, limit: int
) -> tuple[list[dict[str, Any]], bool, Keyset | None]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, more = await uow.human_tasks.page(status, after=after, limit=limit)
    next_key = (rows[-1].created_at, rows[-1].id) if more and rows else None
    return [task_fields(r) for r in rows], more, next_key
