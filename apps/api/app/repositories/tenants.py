"""The tenant's brand profile (S16 T14; DATABASE.md:216-229): `app_rw` may UPDATE only `name`,
`tone`, `guidelines`, `glossary`, (0030, ADR-0031) `owner_self_approval` and (0038, client item 20)
`auto_fix_enabled`, so this repository writes nothing else."""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import update

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.tenancy import Tenant
from app.repositories.base import TenantScopedRepository

PROFILE_FIELDS: Final = frozenset({"name", "tone", "guidelines", "glossary"})
# ADR-0031: the brand's approval setting; only an owner changes it (services/tenants.py)
SETTING_FIELDS: Final = frozenset({"owner_self_approval", "auto_fix_enabled"})
WRITABLE_FIELDS: Final = PROFILE_FIELDS | SETTING_FIELDS


class TenantProfileRepository(TenantScopedRepository[Tenant]):
    model = Tenant

    async def get_profile(self) -> Tenant | None:
        return (await self.db.execute(self._select())).scalar_one_or_none()

    async def lock_profile(self) -> Tenant | None:
        stmt = self._select().with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def owner_self_approval(self) -> bool:
        """ADR-0031: whether an owner may approve its own work in this tenant (false when the
        row is not visible, so a missing row never grants anything)."""
        stmt = self._select().with_only_columns(Tenant.owner_self_approval)
        return bool((await self.db.execute(stmt)).scalar_one_or_none())

    async def auto_fix_enabled(self) -> bool:
        """Client item 20: whether Kuinn may apply safe listing fixes by itself in this tenant
        (false when the row is not visible, so a missing row never lets a fix through)."""
        stmt = self._select().with_only_columns(Tenant.auto_fix_enabled)
        return bool((await self.db.execute(stmt)).scalar_one_or_none())

    async def update_profile(self, fields: dict[str, Any]) -> Tenant:
        bad = sorted(set(fields) - WRITABLE_FIELDS)
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
