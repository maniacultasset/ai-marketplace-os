"""`CategoryTemplateRepository`: the current category template (DATABASE.md:530-535; S14b CD4).

`category_templates` is platform-global (no `tenant_id`, SELECT-only for the app roles), so this is
not a `TenantScopedRepository` (CODE-PLAN S12 T3 `PLATFORM_READERS`). `current` reads the one
`is_current` row of a (marketplace, category) pair (`ct_one_current`) as the wire template;
`pinned` reads one row by id, current or not (the template a listing version pinned, S20 T18).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from mkos_schemas.jobs import CategoryTemplate as WireTemplate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.platform import CategoryTemplate


def takes_current(
    *,
    pinned_id: UUID | None,
    pinned_category: str | None,
    current_id: UUID | None,
    listing_category: str,
    accepted: bool,
) -> bool:
    """Prod feedback 18 Nov: a listing version is built with its category's current template
    instead of the older one it pinned when the marketplace never accepted the listing
    (`accepted`: a published or live version) and the pin is of the listing's own category.
    A live listing keeps its pin (its update payload does not change under it); a recategorised
    one keeps its old pin, so publish still asks for the regenerate. The rules check already
    judges every version against the current template, so the send then matches what was
    approved (a T-shirt pinned before Amazon `style` was mapped sends its `style`)."""
    if pinned_id is None or current_id is None or pinned_id == current_id or accepted:
        return False
    return pinned_category == listing_category


@dataclass(frozen=True, slots=True)
class CurrentTemplate:
    id: UUID
    version: int
    template: WireTemplate


@dataclass(frozen=True, slots=True)
class CurrentCategory:
    """SI14: one category with a current template (any marketplace)."""

    category: str
    version: int
    required_attributes: tuple[str, ...]
    # required, recommended then optional keys (union over marketplaces): what the category holds
    attribute_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CurrentMapping:
    """Client feedback 11 Nov: one current template, as the category picker shows it."""

    marketplace: str
    category: str
    version: int
    product_type: str
    external_category_id: str | None


@dataclass(frozen=True, slots=True)
class PinnedTemplate:
    id: UUID
    version: int
    product_type: str | None
    external_category_id: str | None
    template: WireTemplate


def _wire(marketplace: str, category: str, row: Any) -> WireTemplate:
    attributes = dict(row.attributes or {})
    return WireTemplate.model_validate(
        {
            **attributes,
            "marketplace": marketplace,
            "category": category,
            "external_category_id": row.external_category_id,
        }
    )


class CategoryTemplateRepository:
    model: ClassVar[type[CategoryTemplate]] = CategoryTemplate

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def is_mapped(self, marketplace: str, category: str) -> bool:
        """SI9 recategorize: a current template exists for the pair, and for Amazon it names a
        product type (an unmapped one is refused at publish, `CATEGORY_NOT_MAPPED`)."""
        stmt = select(CategoryTemplate.id, CategoryTemplate.product_type).where(
            CategoryTemplate.marketplace == marketplace,
            CategoryTemplate.category == category,
            CategoryTemplate.is_current.is_(True),
        )
        row = (await self.db.execute(stmt)).one_or_none()
        if row is None:
            return False
        return marketplace != "amazon" or bool(row.product_type)

    async def current(self, marketplace: str, category: str) -> CurrentTemplate | None:
        stmt = select(
            CategoryTemplate.id,
            CategoryTemplate.version,
            CategoryTemplate.external_category_id,
            CategoryTemplate.attributes,
        ).where(
            CategoryTemplate.marketplace == marketplace,
            CategoryTemplate.category == category,
            CategoryTemplate.is_current.is_(True),
        )
        row = (await self.db.execute(stmt)).one_or_none()
        if row is None:
            return None
        template = _wire(marketplace, category, row)
        return CurrentTemplate(id=row.id, version=row.version, template=template)

    async def categories_for_product_type(self, marketplace: str, product_type: str) -> list[str]:
        """SI13: the categories whose current template maps to `product_type` (sorted), for the
        reassignment item's suggestion."""
        stmt = (
            select(CategoryTemplate.category)
            .where(
                CategoryTemplate.marketplace == marketplace,
                CategoryTemplate.product_type == product_type,
                CategoryTemplate.is_current.is_(True),
            )
            .order_by(CategoryTemplate.category)
            .limit(5)
        )
        return [str(c) for c in (await self.db.execute(stmt)).scalars()]

    async def pinned(self, template_id: UUID) -> PinnedTemplate | None:
        t = CategoryTemplate
        stmt = select(
            t.id,
            t.marketplace,
            t.category,
            t.version,
            t.product_type,
            t.external_category_id,
            t.attributes,
        ).where(t.id == template_id)
        row = (await self.db.execute(stmt)).one_or_none()
        if row is None:
            return None
        return PinnedTemplate(
            id=row.id,
            version=row.version,
            product_type=row.product_type or None,
            external_category_id=row.external_category_id,
            template=_wire(row.marketplace, row.category, row),
        )

    async def current_mappings(self) -> list[CurrentMapping]:
        """Every current template's marketplace, category, version, product type and external
        category id, sorted by category then marketplace. Platform rows, so no tenant filter."""
        t = CategoryTemplate
        stmt = (
            select(t.marketplace, t.category, t.version, t.product_type, t.external_category_id)
            .where(t.is_current.is_(True))
            .order_by(t.category, t.marketplace)
        )
        return [
            CurrentMapping(
                marketplace=str(row.marketplace),
                category=str(row.category),
                version=int(row.version),
                product_type=str(row.product_type or ""),
                external_category_id=row.external_category_id,
            )
            for row in (await self.db.execute(stmt)).all()
        ]

    async def current_categories(self) -> list[CurrentCategory]:
        """SI14: every category with a current template on any marketplace, sorted, with the
        highest current version and the union of its required attribute keys (the classifier's
        offer). Platform rows, so no tenant filter."""
        stmt = (
            select(CategoryTemplate.category, CategoryTemplate.version, CategoryTemplate.attributes)
            .where(CategoryTemplate.is_current.is_(True))
            .order_by(CategoryTemplate.category, CategoryTemplate.marketplace)
        )
        merged: dict[str, CurrentCategory] = {}
        for category, version, attributes in (await self.db.execute(stmt)).all():
            attrs = attributes or {}
            required = tuple(attrs.get("required_attributes") or ())
            every = tuple(
                dict.fromkeys(
                    str(k)
                    for name in (
                        "required_attributes",
                        "recommended_attributes",
                        "optional_attributes",
                    )
                    for k in attrs.get(name) or ()
                )
            )
            seen = merged.get(category)
            if seen is None:
                merged[category] = CurrentCategory(str(category), int(version), required, every)
            else:
                keys = seen.required_attributes + tuple(
                    k for k in required if k not in seen.required_attributes
                )
                union = tuple(dict.fromkeys((*seen.attribute_keys, *every)))
                merged[category] = CurrentCategory(
                    seen.category, max(seen.version, int(version)), keys, union
                )
        return [merged[name] for name in sorted(merged)]
