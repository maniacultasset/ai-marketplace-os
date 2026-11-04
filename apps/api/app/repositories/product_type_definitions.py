"""`ProductTypeDefinitionRepository`: the current Amazon Product Type Definition (DW2 T8; C6).

`product_type_definitions` is platform-global (no `tenant_id`, SELECT-only for the app roles), so
this is not a `TenantScopedRepository` (`PLATFORM_READERS`). `current` reads the one `is_current`
row of a key (`ptd_one_current`) as a light `ProductTypeSchema`: it never selects `schema_json`
(1-3 MB per definition). `schema_json` reads the schema by id, and is called only on a validator
cache miss (the `load` passed to `schema_check.prepare`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ProductTypeSchema
from app.db.models.platform import ProductTypeDefinition


class ProductTypeDefinitionRepository:
    model: ClassVar[type[ProductTypeDefinition]] = ProductTypeDefinition

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def current(
        self, *, marketplace_id: str, product_type: str, requirements: str, locale: str
    ) -> ProductTypeSchema | None:
        d = ProductTypeDefinition
        stmt = select(
            d.id,
            d.product_type,
            d.product_type_version,
            d.requirements,
            d.requirements_enforced,
            d.schema_digest,
        ).where(
            d.marketplace_id == marketplace_id,
            d.product_type == product_type,
            d.requirements == requirements,
            d.locale == locale,
            d.is_current.is_(True),
        )
        row = (await self.db.execute(stmt)).one_or_none()
        if row is None:
            return None
        return ProductTypeSchema(
            definition_id=row.id,
            product_type=row.product_type,
            product_type_version=row.product_type_version,
            requirements=row.requirements,
            enforced=row.requirements_enforced == "ENFORCED",
            digest=bytes(row.schema_digest),
        )

    async def schema_json(self, definition_id: UUID) -> Mapping[str, Any]:
        stmt = select(ProductTypeDefinition.schema_json).where(
            ProductTypeDefinition.id == definition_id
        )
        found = (await self.db.execute(stmt)).scalar_one_or_none()
        if found is None:
            raise LookupError("no such product type definition")
        return dict(found)
