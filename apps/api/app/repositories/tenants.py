"""The tenant's brand profile (S16 T14; DATABASE.md:216-229): `app_rw` may UPDATE only `name`,
`tone`, `guidelines` and `glossary`, so this repository writes nothing else."""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import update

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.tenancy import Tenant
from app.repositories.base import TenantScopedRepository

PROFILE_FIELDS: Final = frozenset({"name", "tone", "guidelines", "glossary"})


class TenantProfileRepository(TenantScopedRepository[Tenant]):
    model = Tenant

    async def get_profile(self) -> Tenant | None:
        return (await self.db.execute(self._select())).scalar_one_or_none()

    async def lock_profile(self) -> Tenant | None:
        stmt = self._select().with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def update_profile(self, fields: dict[str, Any]) -> Tenant:
        bad = sorted(set(fields) - PROFILE_FIELDS)
        if bad or not fields:
            raise ValueError(f"update_profile refuses column(s) {bad}")
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("tenants write for another tenant's unit of work")
        stmt = self._scoped_write(
            update(Tenant).where(self._tenant_criterion(include_platform=False)).values(**fields)
        ).returning(Tenant)
        row = (
            await self.db.execute(stmt, execution_options={"populate_existing": True})
        ).scalar_one()
        return row
