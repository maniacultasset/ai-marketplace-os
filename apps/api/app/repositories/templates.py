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


@dataclass(frozen=True, slots=True)
class CurrentTemplate:
    id: UUID
    version: int
    template: WireTemplate


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
