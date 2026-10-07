"""`CategoryTemplateRepository`: the current category template (DATABASE.md:530-535; S14b CD4).

`category_templates` is platform-global (no `tenant_id`, SELECT-only for the app roles), so this is
not a `TenantScopedRepository` (CODE-PLAN S12 T3 `PLATFORM_READERS`). `current` reads the one
`is_current` row of a (marketplace, category) pair (`ct_one_current`) as the wire template.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar
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
        attributes = dict(row.attributes or {})
        template = WireTemplate.model_validate(
            {
                **attributes,
                "marketplace": marketplace,
                "category": category,
                "external_category_id": row.external_category_id,
            }
        )
        return CurrentTemplate(id=row.id, version=row.version, template=template)
