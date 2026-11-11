"""`SkuCounterRepository` (0029, client feedback 11 Nov): the per-tenant SKU numbers behind
"Make one for me". One atomic upsert per number, so two requests never get the same one, and a
number once handed out is never handed out again (the counter only grows)."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.catalog import SkuCounter
from app.repositories.base import TenantScopedRepository

__all__ = ["SkuCounterRepository"]


class SkuCounterRepository(TenantScopedRepository[SkuCounter]):
    model = SkuCounter

    async def take_next(self, code: str) -> int:
        """The next number for `code` in this tenant (1 for a new code). The row lock the upsert
        takes holds a concurrent caller until this transaction ends, so each gets its own."""
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("a SKU counter write for another tenant's unit of work")
        stmt = pg_insert(SkuCounter).values(tenant_id=self.tenant_id, code=code, last_value=1)
        table = SkuCounter.__table__
        upsert = stmt.on_conflict_do_update(
            index_elements=[table.c.tenant_id, table.c.code],
            set_={"last_value": table.c.last_value + 1},
        ).returning(table.c.last_value)
        value: int = (await self.db.execute(upsert)).scalar_one()
        return value
