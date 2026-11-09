"""The seller's category write settles the suggestion (SI14, AC6).

`settle_on_category_write(uow, user_id, row, new_category)` runs inside the unit of work of a
category write (`PATCH /products/{id}` and `POST /products/{id}/recategorize`):
- a stored `suggested` suggestion becomes `confirmed` when the new category is one of its
  candidates, else `overridden` (kept for the quality read: how often the seller agreed);
- the product's live `category_review` task is answered with `{category}` by the seller and then
  applied (the 0004 guard allows only open -> answered -> applied).
No lamps check here (PLAN E): only a new lamps listing is gated (T23). Nothing is written when
there is no suggestion and no task.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.repositories.uow import TenantUnitOfWork

log = get_logger(__name__)


async def settle_on_category_write(
    uow: TenantUnitOfWork, user_id: uuid.UUID, row: Any, new_category: str
) -> None:
    stored = row.category_suggestion
    if isinstance(stored, Mapping) and stored.get("status") == "suggested":
        candidates = {
            c.get("category") for c in stored.get("candidates") or [] if isinstance(c, Mapping)
        }
        status = "confirmed" if new_category in candidates else "overridden"
        await uow.products.write_category_suggestion(
            row.id, {**stored, "status": status, "at": datetime.now(UTC).isoformat()}
        )
        log.info("category_suggestion.settled", product_id=str(row.id), status=status)
    task = await uow.human_tasks.live_category_review(row.id)
    if task is not None and task.status == "open":
        applied = await uow.human_tasks.answer_and_apply(
            task.id, answer={"category": new_category}, user_id=user_id
        )
        log.info(
            "category_review.applied", product_id=str(row.id), task_id=str(task.id), applied=applied
        )
