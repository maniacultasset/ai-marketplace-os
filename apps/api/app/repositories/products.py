"""`ProductRepository` (CODE-PLAN S12 T5; DATABASE.md:402-424; BACKEND-ARCHITECTURE.md:1410).

Reads see only live rows (`deleted_at IS NULL`) of the repository's tenant. `list_page` is the
keyset page over `(created_at DESC, id DESC)` (the `ix_products_keyset` index): `limit + 1` rows
are read to learn `has_more`, and there is no OFFSET. `create` builds the row, so services never
import `app.db`.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, cast, func, literal, literal_column, tuple_
from sqlalchemy.dialects.postgresql import JSONB
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
# SI1 D9(e): an import row with no media cell keeps the stored URLs (an empty list is "absent")
KEEP_WHEN_EMPTY = frozenset({"source_image_urls"})
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

    async def photo_rejections(self, product_id: uuid.UUID) -> dict[str, str]:
        """SI9 B5: `{key: rejected value}`, read on its own (never part of `ProductFacts`, so the
        facts digest is unchanged, CP-D12); `{}` for an unknown product."""
        stmt = (
            self._select()
            .where(Product.id == product_id)
            .with_only_columns(Product.photo_rejections)
        )
        found = (await self.db.execute(stmt)).scalar_one_or_none()
        return {str(k): str(v) for k, v in (found or {}).items()}

    async def get_for_update(self, id: uuid.UUID) -> Product | None:
        """The live row, locked for the rest of the transaction (SI1 PATCH)."""
        if not isinstance(id, uuid.UUID):
            raise ValueError(f"id must be a UUID, got {type(id).__name__}")
        stmt = (
            self._select()
            .where(Product.id == id, Product.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def live_child_ids(self, style_id: uuid.UUID) -> builtins.list[uuid.UUID]:
        """The live children of `style_id`, in id order (SI1 T20: each is a sellable unit)."""
        stmt = (
            self._select()
            .where(Product.parent_product_id == style_id, Product.deleted_at.is_(None))
            .with_only_columns(Product.id)
            .order_by(Product.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def child_prices(
        self, style_id: uuid.UUID
    ) -> builtins.list[tuple[Decimal | None, Decimal | None]]:
        """`(selling_price, mrp)` of each live child of `style_id` (CP14)."""
        stmt = (
            self._select()
            .where(Product.parent_product_id == style_id, Product.deleted_at.is_(None))
            .with_only_columns(Product.selling_price, Product.mrp)
        )
        return [(row[0], row[1]) for row in (await self.db.execute(stmt)).all()]

    async def money_of(self, product_id: uuid.UUID) -> tuple[Decimal | None, Decimal | None]:
        """The stored `(selling_price, mrp)` of a live product (a fresh read, never the session's
        cached row: FactWriter writes these columns with Core statements)."""
        stmt = (
            self._select()
            .where(Product.id == product_id, Product.deleted_at.is_(None))
            .with_only_columns(Product.selling_price, Product.mrp)
        )
        found = (await self.db.execute(stmt)).one()
        return (found[0], found[1])

    async def child_money(
        self, style_id: uuid.UUID
    ) -> builtins.list[tuple[str, Decimal | None, Decimal | None]]:
        """`(sku, selling_price, mrp)` of each live child of `style_id`, in SKU order (CP14 for
        a CSV style row)."""
        stmt = (
            self._select()
            .where(Product.parent_product_id == style_id, Product.deleted_at.is_(None))
            .with_only_columns(Product.sku, Product.selling_price, Product.mrp)
            .order_by(Product.sku)
        )
        return [(row[0], row[1], row[2]) for row in (await self.db.execute(stmt)).all()]

    async def family_of_sku(self, sku: str) -> uuid.UUID | None:
        """The stored `parent_product_id` of `sku` (soft-deleted rows included: an upsert
        revives them), or None for a new SKU or a style/standalone product."""
        stmt = self._select().where(Product.sku == sku).with_only_columns(Product.parent_product_id)
        found: uuid.UUID | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def family_role_of_sku(self, sku: str) -> tuple[uuid.UUID | None, str | None] | None:
        """The stored `(parent_product_id, variation_theme)` of `sku` (soft-deleted rows
        included), or None for a new SKU: a child has a parent, a family's style a theme."""
        stmt = (
            self._select()
            .where(Product.sku == sku)
            .with_only_columns(Product.parent_product_id, Product.variation_theme)
        )
        found = (await self.db.execute(stmt)).one_or_none()
        return None if found is None else (found[0], found[1])

    async def id_of_sku(self, sku: str) -> uuid.UUID | None:
        stmt = self._select().where(Product.sku == sku).with_only_columns(Product.id)
        found: uuid.UUID | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def extend_size_run(self, id: uuid.UUID, sizes: Sequence[str]) -> None:
        """Append the sizes the stored `size_run` lacks (nothing is written when none is new),
        leaving every identity column as it is."""
        stored = await self.size_run_of(id)
        new = [size for size in sizes if size not in stored]
        if new:
            await self.update_one(id, size_run=[*stored, *new])

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
        id: uuid.UUID | None = None,
    ) -> Product:
        product = Product(
            sku=sku,
            title=title,
            category=category,
            attributes=dict(attributes),
            provenance=dict(provenance),
            created_by=created_by,
        )
        if id is not None:  # SI1 D3: the id minted by the upload URL
            product.id = id
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
        empty = cast(literal("[]"), JSONB)
        new_values = {
            name: (
                func.coalesce(stmt.excluded[name], table.c[name])
                if name in KEEP_WHEN_BLANK
                else (
                    case((stmt.excluded[name] == empty, table.c[name]), else_=stmt.excluded[name])
                    if name in KEEP_WHEN_EMPTY
                    else stmt.excluded[name]
                )
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
