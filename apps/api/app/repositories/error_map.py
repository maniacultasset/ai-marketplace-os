"""`ErrorMapRepository`: the enabled marketplace error-map rows (DATABASE.md:546-557; S20 T4).

`marketplace_error_map` is platform-global (no `tenant_id`, SELECT-only for the app roles,
written only by the migrator's `errors load`), so this is a platform reader, not a
`TenantScopedRepository` (CODE-PLAN S12 T3 `PLATFORM_READERS`, reason "platform-global table").
"""

from __future__ import annotations

from typing import ClassVar

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, FixAction, RetryClass, Severity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.error_map import ErrorMapRow
from app.db.models.platform import MarketplaceErrorMap


class ErrorMapRepository:
    model: ClassVar[type[MarketplaceErrorMap]] = MarketplaceErrorMap

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def enabled_rows(self, marketplace: str) -> list[ErrorMapRow]:
        m = MarketplaceErrorMap
        stmt = (
            select(m)
            .where(m.marketplace == marketplace, m.enabled.is_(True))
            .order_by(m.match_on, m.raw_value, m.version)
        )
        return [
            ErrorMapRow(
                marketplace=row.marketplace,
                match_on=row.match_on,
                raw_value=row.raw_value,
                version=row.version,
                error_code=ErrorCode(row.error_code),
                error_class=ErrorClass(row.error_class),
                retry_class=RetryClass(row.retry_class),
                severity=Severity(row.severity),
                auto_fixable=row.auto_fixable,
                fix_action=FixAction(row.fix_action) if row.fix_action else None,
                field_hint=row.field_hint,
                plain_text=row.plain_text,
                seller_action=row.seller_action,
                enabled=row.enabled,
            )
            for row in (await self.db.execute(stmt)).scalars()
        ]
