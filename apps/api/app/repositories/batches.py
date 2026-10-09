"""`batches` (DATABASE.md §5.3; S19 T3): one row per batch request (approve, reject, ...).

`item_count` is the request's size, so the review bulk backstop (0004 `review_bulk_backstop`) sees
whether an approve came in a batch of more than one.
"""

from __future__ import annotations

import uuid

from app.db.models.catalog_assets import Batch
from app.repositories.base import TenantScopedRepository


class BatchRepository(TenantScopedRepository[Batch]):
    model = Batch

    async def insert(
        self,
        kind: str,
        item_count: int,
        created_by: uuid.UUID,
        *,
        request_id: str | None = None,
    ) -> Batch:
        """A conflict (the kind or count CHECK) comes back as `ConflictError`."""
        return await self.add(
            Batch(
                kind=kind,
                item_count=item_count,
                created_by_user_id=created_by,
                request_id=request_id,
            )
        )
