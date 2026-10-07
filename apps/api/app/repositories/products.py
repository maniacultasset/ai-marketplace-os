"""`ProductRepository` (CODE-PLAN S12 T5; DATABASE.md:402-424; BACKEND-ARCHITECTURE.md:1410).

Reads see only live rows (`deleted_at IS NULL`) of the repository's tenant. `list_page` is the
keyset page over `(created_at DESC, id DESC)` (the `ix_products_keyset` index): `limit + 1` rows
are read to learn `has_more`, and there is no OFFSET. `create` builds the row, so services never
import `app.db`.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import literal, tuple_

from app.db.models.catalog import Product
from app.repositories.base import TenantScopedRepository

PAGE_LIMIT_MIN = 1
PAGE_LIMIT_MAX = 200


class ProductRepository(TenantScopedRepository[Product]):
    model = Product

    async def get_by_id(self, id: uuid.UUID) -> Product | None:
        if not isinstance(id, uuid.UUID):
            raise ValueError(f"id must be a UUID, got {type(id).__name__}")
        stmt = self._select().where(Product.id == id, Product.deleted_at.is_(None))
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self, *, limit: int, after: tuple[datetime, uuid.UUID] | None
    ) -> tuple[builtins.list[Product], bool]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an int")
        if not PAGE_LIMIT_MIN <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be in {PAGE_LIMIT_MIN}..{PAGE_LIMIT_MAX}")
        stmt = self._select().where(Product.deleted_at.is_(None))
        if after is not None:
            created_at, row_id = after
            bound = tuple_(
                literal(created_at, Product.created_at.type), literal(row_id, Product.id.type)
            )
            stmt = stmt.where(tuple_(Product.created_at, Product.id) < bound)
        stmt = stmt.order_by(Product.created_at.desc(), Product.id.desc()).limit(limit + 1)
        rows = builtins.list((await self.db.execute(stmt)).scalars())
        return rows[:limit], len(rows) > limit

    async def create(
        self,
        *,
        sku: str,
        title: str,
        category: str,
        attributes: Mapping[str, Any],
        provenance: Mapping[str, Any],
        created_by: uuid.UUID | None,
    ) -> Product:
        product = Product(
            sku=sku,
            title=title,
            category=category,
            attributes=dict(attributes),
            provenance=dict(provenance),
            created_by=created_by,
        )
        added = await self.add(product)
        # server defaults (`status`, `created_at`, `updated_at`) are read back with the row
        await self.db.refresh(added)
        return added
