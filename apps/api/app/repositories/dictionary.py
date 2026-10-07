"""`DictionaryRevisionRepository`: the dictionary half of `rules_version` (DATABASE.md:507; S14b C6).

`rules_version` is the highest revision over every fetched rule row **and** all
`attribute_definitions` rows plus the `category_attributes` rows of the category, disabled rows
included (a criticality flip or a category toggle must move it). Both tables are platform-global
and SELECT-only for the app roles, so this is a platform reader (`PLATFORM_READERS`).
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.platform import AttributeDefinition, CategoryAttribute


class DictionaryRevisionRepository:
    model: ClassVar[type[AttributeDefinition]] = AttributeDefinition

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def revisions(self, category: str) -> tuple[int, int]:
        """`(max attribute_definitions.revision, max category_attributes.revision for category)`,
        0 when empty, in one statement."""
        definitions = select(func.coalesce(func.max(AttributeDefinition.revision), 0))
        category_rows = select(func.coalesce(func.max(CategoryAttribute.revision), 0)).where(
            CategoryAttribute.category == category
        )
        stmt = select(definitions.scalar_subquery(), category_rows.scalar_subquery())
        row = (await self.db.execute(stmt)).one()
        return int(row[0]), int(row[1])
