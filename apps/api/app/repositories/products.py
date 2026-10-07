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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, literal, literal_column, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.catalog import Product
from app.repositories.base import TenantScopedRepository

PAGE_LIMIT_MIN = 1
PAGE_LIMIT_MAX = 200
# The identity and family columns an import row sets; facts go through the FactWriter.
UPSERT_COLUMNS = (
    "title",
    "category",
    "brand_name",
    "sub_category",
    "product_type",
    "gender",
    "parent_product_id",
    "variation_theme",
    "variant_attributes",
    "source_image_urls",
)
KEEP_WHEN_BLANK = frozenset({"brand_name", "sub_category", "product_type", "gender"})
# column → its provenance key (the same pairs `attribute_columns` holds)
UPSERT_FACT_KEYS = {
    "title": "title",
    "category": "category",
    "brand_name": "brand_name",
    "sub_category": "sub_category",
    "product_type": "product_type",
    "gender": "gender",
}


@dataclass(frozen=True, slots=True)
class ProductUpsert:
    """One import row's product: identity, classification and family columns, and the seller
    stamp their `provenance` keys carry. `size_run` (a style's) is merged with the stored one."""

    sku: str
    title: str
    category: str
    stamp: Mapping[str, Any]
    brand_name: str | None = None
    sub_category: str | None = None
    product_type: str | None = None
    gender: str | None = None
    variation_theme: str | None = None
    size_run: tuple[str, ...] | None = None
    variant_attributes: Mapping[str, str] = field(default_factory=dict)
    source_image_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Upserted:
    id: uuid.UUID
    inserted: bool


class ProductRepository(TenantScopedRepository[Product]):
    model = Product

    async def get_by_id(self, id: uuid.UUID) -> Product | None:
        if not isinstance(id, uuid.UUID):
            raise ValueError(f"id must be a UUID, got {type(id).__name__}")
        stmt = self._select().where(Product.id == id, Product.deleted_at.is_(None))
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def size_run_of(self, id: uuid.UUID) -> builtins.list[str]:
        """The stored `size_run` read as a column (never a cached ORM row)."""
        stmt = self._select().where(Product.id == id).with_only_columns(Product.size_run)
        found = (await self.db.execute(stmt)).scalar_one_or_none()
        return builtins.list(found or [])

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

    async def upsert_by_sku(
        self, row: ProductUpsert, *, parent_product_id: uuid.UUID | None = None
    ) -> Upserted:
        """Insert the product, or update the stored one on `(tenant_id, sku)` (a soft-deleted
        row is revived, DATABASE.md:407). An unchanged live row is not written at all, so a
        re-import never bumps `updated_at`. The provenance of every set identity column is
        merged in, key by key. A style's `size_run` keeps its stored sizes, new ones appended."""
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("products upsert for another tenant's unit of work")
        size_run: list[str] | None = None
        if row.size_run is not None:
            stored = (
                await self.db.execute(
                    self._select().where(Product.sku == row.sku).with_only_columns(Product.size_run)
                )
            ).scalar_one_or_none()
            size_run = list(stored or [])
            size_run += [size for size in row.size_run if size not in size_run]
        values: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "sku": row.sku,
            "title": row.title,
            "category": row.category,
            "brand_name": row.brand_name,
            "sub_category": row.sub_category,
            "product_type": row.product_type,
            "gender": row.gender,
            "parent_product_id": parent_product_id,
            "variation_theme": row.variation_theme,
            "size_run": size_run,
            "variant_attributes": dict(row.variant_attributes),
            "source_image_urls": list(row.source_image_urls),
            "provenance": {
                key: dict(row.stamp)
                for column, key in UPSERT_FACT_KEYS.items()
                if getattr(row, column) is not None
            },
        }
        stmt = pg_insert(Product).values(**values)
        table = Product.__table__
        columns = (*UPSERT_COLUMNS, "size_run")
        # a blank optional cell keeps the stored value (blank means absent, not "clear it")
        new_values = {
            name: (
                func.coalesce(stmt.excluded[name], table.c[name])
                if name in KEEP_WHEN_BLANK
                else stmt.excluded[name]
            )
            for name in columns
        }
        changed = tuple_(*(table.c[name] for name in columns)).is_distinct_from(
            tuple_(*new_values.values())
        ) | table.c.deleted_at.is_not(None)
        upsert = stmt.on_conflict_do_update(
            constraint="products_tenant_sku_idx",
            set_={
                **new_values,
                "deleted_at": None,
                "provenance": table.c.provenance.op("||")(stmt.excluded.provenance),
            },
            where=changed,
        ).returning(table.c.id, literal_column("(xmax = 0)"))
        written = (await self.db.execute(upsert)).first()
        if written is not None:
            return Upserted(written[0], bool(written[1]))
        found = (
            await self.db.execute(
                self._select().where(Product.sku == row.sku).with_only_columns(Product.id)
            )
        ).scalar_one()
        return Upserted(found, False)
