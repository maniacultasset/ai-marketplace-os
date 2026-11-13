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

from sqlalchemy import case, cast, func, literal, literal_column, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.catalog import Product
from app.db.models.listings import Listing
from app.repositories.base import TenantScopedRepository
from app.repositories.home import in_bucket

PAGE_LIMIT_MIN = 1
# SI14: the seller's decision on a suggestion; a job never writes over it
SETTLED_SUGGESTION_STATUSES = frozenset({"confirmed", "overridden"})
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
class SkuHolder:
    """The product row that holds a SKU (client feedback 11 Nov: the live SKU check)."""

    id: uuid.UUID
    title: str
    parent_product_id: uuid.UUID | None
    deleted: bool
    # set on a family's style (it has sizes or colours); a plain CSV row cannot update it
    variation_theme: str | None = None


@dataclass(frozen=True, slots=True)
class Upserted:
    id: uuid.UUID
    inserted: bool
    revived: bool = False  # the row was soft-deleted (a product delete) before this write


class ProductRepository(TenantScopedRepository[Product]):
    model = Product

    async def get_by_id(self, id: uuid.UUID) -> Product | None:
        if not isinstance(id, uuid.UUID):
            raise ValueError(f"id must be a UUID, got {type(id).__name__}")
        stmt = self._select().where(Product.id == id, Product.deleted_at.is_(None))
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def soft_delete_many(self, ids: Sequence[uuid.UUID]) -> builtins.list[uuid.UUID]:
        """Product delete: stamp `deleted_at` on these live rows (children before their style:
        the variant guard refuses a style with live children). A CSV re-import of the SKU
        revives the row (`upsert_by_sku`). The ids written, in the order given."""
        out: builtins.list[uuid.UUID] = []
        for product_id in ids:
            stmt = self._scoped_write(
                update(Product)
                .where(
                    self._tenant_criterion(include_platform=False),
                    Product.id == product_id,
                    Product.deleted_at.is_(None),
                )
                .values(deleted_at=func.now())
            ).returning(Product.id)
            found = (await self.db.execute(stmt)).scalar_one_or_none()
            if found is not None:
                out.append(found)
        return out

    async def set_archived(
        self, ids: Sequence[uuid.UUID], archived: bool
    ) -> builtins.list[uuid.UUID]:
        """Archive (0039) or unarchive these live rows: stamp or clear `archived_at` where it
        changes. The ids written."""
        if not ids:
            return []
        stmt = self._scoped_write(
            update(Product)
            .where(
                self._tenant_criterion(include_platform=False),
                Product.id.in_(ids),
                Product.deleted_at.is_(None),
                Product.archived_at.is_(None) if archived else Product.archived_at.is_not(None),
            )
            .values(archived_at=func.now() if archived else None)
        ).returning(Product.id)
        return sorted((await self.db.execute(stmt)).scalars())

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

    async def write_category_suggestion(
        self,
        product_id: uuid.UUID,
        value: Mapping[str, Any] | None,
        *,
        keep_settled: bool = False,
    ) -> bool:
        """SI14: store the classifier's suggestion (only this column; never the category, the
        facts or provenance). With `keep_settled`, a stored `confirmed` or `overridden` value
        (the seller's decision) is left as it is. False when nothing was written."""
        criteria = [
            self._tenant_criterion(include_platform=False),
            Product.id == product_id,
            Product.deleted_at.is_(None),
        ]
        if keep_settled:
            stored = Product.category_suggestion["status"].astext
            criteria.append(
                or_(stored.is_(None), stored.not_in(sorted(SETTLED_SUGGESTION_STATUSES)))
            )
        stmt = self._scoped_write(
            update(Product)
            .where(*criteria)
            .values(category_suggestion=None if value is None else dict(value))
        )
        return int(getattr(await self.db.execute(stmt), "rowcount", 0) or 0) == 1

    async def live_child_ids(self, style_id: uuid.UUID) -> builtins.list[uuid.UUID]:
        """The live children of `style_id`, in id order (SI1 T20: each is a sellable unit)."""
        stmt = (
            self._select()
            .where(Product.parent_product_id == style_id, Product.deleted_at.is_(None))
            .with_only_columns(Product.id)
            .order_by(Product.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def child_sizes(self, style_id: uuid.UUID) -> builtins.list[str]:
        """The `variant_attributes.size` of each live child of `style_id` that has one, in id
        order (the photo views' size chart check)."""
        size = Product.variant_attributes["size"].astext
        stmt = (
            self._select()
            .where(
                Product.parent_product_id == style_id,
                Product.deleted_at.is_(None),
                size.is_not(None),
            )
            .with_only_columns(size)
            .order_by(Product.id)
        )
        return [str(v) for v in (await self.db.execute(stmt)).scalars() if v]

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

    async def identifier_taken(self, value: str, exclude_id: uuid.UUID | None) -> bool:
        """SI5: a live product of this tenant (other than `exclude_id`) already holds this
        barcode, compared as `products_identifier_uq` does (left-padded to 14)."""
        stmt = self._select().where(
            func.lpad(Product.identifier_value, 14, "0") == value.zfill(14),
            Product.deleted_at.is_(None),
        )
        if exclude_id is not None:
            stmt = stmt.where(Product.id != exclude_id)
        found = await self.db.execute(stmt.with_only_columns(Product.id).limit(1))
        return found.first() is not None

    async def style_exemptions(self, style_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, bool]:
        """SI5 (CP-D18): per style, whether it has at least one live variant and every live
        variant is GTIN-exempt; one aggregate for the whole list, none for an empty one."""
        out = dict.fromkeys(style_ids, False)
        if not out:
            return out
        stmt = (
            self._select()
            .where(Product.parent_product_id.in_(list(out)), Product.deleted_at.is_(None))
            .with_only_columns(
                Product.parent_product_id,
                func.bool_and(func.coalesce(Product.identifier_status == "exempt", False)),
            )
            .group_by(Product.parent_product_id)
        )
        for style_id, all_exempt in (await self.db.execute(stmt)).all():
            out[style_id] = bool(all_exempt)
        return out

    async def child_identifier_states(
        self, style_id: uuid.UUID
    ) -> builtins.list[tuple[uuid.UUID, str | None, Any]]:
        """SI5: `(id, identifier_status, provenance.identifier)` of each live variant of
        `style_id`, locked `FOR UPDATE` (the fan-out writes them)."""
        stmt = (
            self._select()
            .where(Product.parent_product_id == style_id, Product.deleted_at.is_(None))
            .with_only_columns(
                Product.id, Product.identifier_status, Product.provenance["identifier"]
            )
            .order_by(Product.id)
            .with_for_update(of=Product)
        )
        return [(row[0], row[1], row[2]) for row in (await self.db.execute(stmt)).all()]

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

    async def category_of_sku(self, sku: str) -> str | None:
        """The stored category of `sku` (None when the SKU is new or has no category)."""
        stmt = self._select().where(Product.sku == sku).with_only_columns(Product.category)
        found: str | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def holder_of_sku(self, sku: str) -> SkuHolder | None:
        """The row holding `sku` in this tenant (soft-deleted rows included: the SKU unique
        covers them), or None when the SKU is free. Styles and variants share the one unique."""
        stmt = (
            self._select()
            .where(Product.sku == sku)
            .with_only_columns(
                Product.id,
                Product.title,
                Product.parent_product_id,
                Product.deleted_at,
                Product.variation_theme,
            )
        )
        found = (await self.db.execute(stmt)).one_or_none()
        if found is None:
            return None
        return SkuHolder(
            id=found[0],
            title=found[1],
            parent_product_id=found[2],
            deleted=found[3] is not None,
            variation_theme=found[4],
        )

    async def holders_of_skus(self, skus: Sequence[str]) -> dict[str, SkuHolder]:
        """`holder_of_sku` for many SKUs in one select (client feedback 11 Nov, item 3: the
        batch check before a variants save or a bulk upload): only the held SKUs are keys."""
        if not skus:
            return {}
        stmt = (
            self._select()
            .where(Product.sku.in_(list(skus)))
            .with_only_columns(
                Product.sku,
                Product.id,
                Product.title,
                Product.parent_product_id,
                Product.deleted_at,
                Product.variation_theme,
            )
        )
        return {
            row[0]: SkuHolder(
                id=row[1],
                title=row[2],
                parent_product_id=row[3],
                deleted=row[4] is not None,
                variation_theme=row[5],
            )
            for row in (await self.db.execute(stmt)).all()
        }

    async def standalone_of_sku_for_update(self, sku: str) -> Product | None:
        """The stored standalone on `sku` (no parent, no theme; soft-deleted included, as an
        upsert revives it), locked for the rest of the transaction; None otherwise."""
        stmt = (
            self._select()
            .where(
                Product.sku == sku,
                Product.parent_product_id.is_(None),
                Product.variation_theme.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

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
        self,
        *,
        limit: int,
        after: tuple[datetime, uuid.UUID] | None,
        listing: str | None = None,
        marketplace: str | None = None,
        archived: bool = False,
    ) -> tuple[builtins.list[Product], bool]:
        """One keyset page, newest first. With `listing` (a Home bucket filter,
        `app.repositories.home.BUCKET_FILTERS`) or `marketplace`, only the products that own a
        listing that is not archived in that bucket and on that marketplace. `archived`: only
        the archived products (0039) instead of the others."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an int")
        if not PAGE_LIMIT_MIN <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be in {PAGE_LIMIT_MIN}..{PAGE_LIMIT_MAX}")
        stmt = self._select().where(
            Product.deleted_at.is_(None),
            Product.archived_at.is_not(None) if archived else Product.archived_at.is_(None),
        )
        if listing is not None or marketplace is not None:
            owned = [
                Listing.tenant_id == Product.tenant_id,
                Listing.product_id == Product.id,
                Listing.archived_at.is_(None),
            ]
            if listing is not None:
                owned.append(in_bucket(listing))
            if marketplace is not None:
                owned.append(Listing.marketplace == marketplace)
            stmt = stmt.where(select(literal(1)).where(*owned).exists())
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
        category: str | None,
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
        # the stored row, if any: its size run (merged below) and whether a product delete
        # soft-deleted it, so the caller can bring its archived listings back
        found_row = (
            await self.db.execute(
                self._select()
                .where(Product.sku == row.sku)
                .with_only_columns(Product.size_run, Product.deleted_at)
            )
        ).first()
        was_deleted = found_row is not None and found_row[1] is not None
        size_run: list[str] | None = None
        if row.size_run is not None:
            stored = None if found_row is None else found_row[0]
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
            return Upserted(written[0], bool(written[1]), was_deleted)
        found = (
            await self.db.execute(
                self._select().where(Product.sku == row.sku).with_only_columns(Product.id)
            )
        ).scalar_one()
        return Upserted(found, False)
