"""`AttributeDefinitionRepository`: the platform attribute dictionary (DATABASE.md:1152-1170).

`attribute_definitions` has no `tenant_id` (platform-global, SELECT-only for the app roles), so
this is not a `TenantScopedRepository`; the tenant guard has nothing to check on it. `resolve`
answers the enabled rows whose `key` or one of whose `input_aliases` is among the given names.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy import any_, bindparam, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Text

from app.db.models.platform import AttributeDefinition


@dataclass(frozen=True, slots=True)
class AttributeDef:
    key: str
    storage: str
    owner_levels: tuple[str, ...]
    input_aliases: tuple[str, ...]


class AttributeDefinitionRepository:
    model: ClassVar[type[AttributeDefinition]] = AttributeDefinition

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def resolve(self, names: Collection[str]) -> list[AttributeDef]:
        wanted = sorted(set(names))
        if not wanted:
            return []
        names_param = bindparam("names", wanted, type_=ARRAY(Text))
        stmt = (
            select(
                AttributeDefinition.key,
                AttributeDefinition.storage,
                AttributeDefinition.owner_levels,
                AttributeDefinition.input_aliases,
            )
            .where(
                AttributeDefinition.enabled.is_(True),
                or_(
                    AttributeDefinition.key == any_(names_param),
                    AttributeDefinition.input_aliases.overlap(names_param),
                ),
            )
            .order_by(AttributeDefinition.key)
        )
        rows = (await self.db.execute(stmt)).all()
        return [
            AttributeDef(
                key=row.key,
                storage=row.storage,
                owner_levels=tuple(row.owner_levels),
                input_aliases=tuple(row.input_aliases),
            )
            for row in rows
        ]
