"""`ListingRepository`: what a marketplace adapter needs to know about one listing (S20 T18).

`preview_source` reads, SELECT only and under the tenant's RLS session: the listing, its current
version, its product, the product's live children (sorted by SKU, a family's send order) and the
template the version pinned (`listing_versions.category_template_id`, current or not). Each table
is read through its own tenant-scoped repository; the template is platform-global. Another
tenant's listing id is `None`, exactly as an unknown one.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.db.models.catalog import Product
from app.db.models.listings import Listing, ListingVersion
from app.repositories.base import TenantScopedRepository
from app.repositories.templates import CategoryTemplateRepository, PinnedTemplate


@dataclass(frozen=True, slots=True)
class SourceVersion:
    id: uuid.UUID
    version: int
    content: Mapping[str, Any]
    category_template_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class SourceProduct:
    id: uuid.UUID
    sku: str
    variation_theme: str | None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    variant_attributes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreviewSource:
    listing_id: uuid.UUID
    marketplace: str
    category: str
    status: str
    product: SourceProduct
    version: SourceVersion | None
    """None while the listing has no current version."""
    children: tuple[SourceProduct, ...] = ()
    template: PinnedTemplate | None = None
    """None when the version pinned no template, or a template with no `product_type`."""


class _Versions(TenantScopedRepository[ListingVersion]):
    model = ListingVersion


class _Products(TenantScopedRepository[Product]):
    model = Product


class ListingRepository(TenantScopedRepository[Listing]):
    model = Listing

    async def preview_source(self, listing_id: uuid.UUID) -> PreviewSource | None:
        lst = Listing
        row = (
            await self.db.execute(
                self._select()
                .where(lst.id == listing_id)
                .with_only_columns(
                    lst.id,
                    lst.marketplace,
                    lst.category,
                    lst.status,
                    lst.product_id,
                    lst.current_version_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        products = _Products(self.db, self.tenant_id)
        product = await _product(products, row.product_id)
        if product is None:
            return None
        version = await self._version(row.id, row.current_version_id)
        template = None
        if version is not None and version.category_template_id is not None:
            pinned = await CategoryTemplateRepository(self.db).pinned(version.category_template_id)
            template = pinned if pinned is not None and pinned.product_type else None
        return PreviewSource(
            listing_id=row.id,
            marketplace=row.marketplace,
            category=row.category,
            status=row.status,
            product=product,
            version=version,
            children=await _children(products, product.id),
            template=template,
        )

    async def _version(
        self, listing_id: uuid.UUID, version_id: uuid.UUID | None
    ) -> SourceVersion | None:
        if version_id is None:
            return None
        v = ListingVersion
        repo = _Versions(self.db, self.tenant_id)
        row = (
            await self.db.execute(
                repo._select()
                .where(v.id == version_id, v.listing_id == listing_id)
                .with_only_columns(v.id, v.version, v.content_json, v.category_template_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return SourceVersion(row.id, row.version, dict(row.content_json), row.category_template_id)


_PRODUCT_COLUMNS = (
    Product.id,
    Product.sku,
    Product.variation_theme,
    Product.attributes,
    Product.variant_attributes,
)


def _source(row: Any) -> SourceProduct:
    return SourceProduct(
        id=row.id,
        sku=row.sku,
        variation_theme=row.variation_theme,
        attributes=dict(row.attributes or {}),
        variant_attributes={str(k): str(v) for k, v in (row.variant_attributes or {}).items()},
    )


async def _product(repo: _Products, product_id: uuid.UUID) -> SourceProduct | None:
    stmt = (
        repo._select()
        .where(Product.id == product_id, Product.deleted_at.is_(None))
        .with_only_columns(*_PRODUCT_COLUMNS)
    )
    row = (await repo.db.execute(stmt)).one_or_none()
    return None if row is None else _source(row)


async def _children(repo: _Products, parent_id: uuid.UUID) -> tuple[SourceProduct, ...]:
    stmt = (
        repo._select()
        .where(Product.parent_product_id == parent_id, Product.deleted_at.is_(None))
        .with_only_columns(*_PRODUCT_COLUMNS)
    )
    rows = (await repo.db.execute(stmt)).all()
    # code-point order, independent of the database collation
    return tuple(sorted((_source(r) for r in rows), key=lambda p: (p.sku, str(p.id))))
