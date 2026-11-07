"""A tenant's copied password reset links (SI5, ADR-0029; revision 0017): one live link per member
(`uq_password_reset_links_live`), 24 hours at most, used or revoked once. The pre-tenant read by
token hash is `UserRepository.reset_link_by_hash` (identity.py, repo rule R7)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert

from app.core.errors import RowCountError
from app.db.models.tenancy import PasswordResetLink
from app.repositories.base import TenantScopedRepository


@dataclass(frozen=True, slots=True)
class ResetLinkRow:
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    issued_by_user_id: uuid.UUID
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    live: bool


_COLUMNS: Final = (
    PasswordResetLink.id,
    PasswordResetLink.tenant_id,
    PasswordResetLink.user_id,
    PasswordResetLink.issued_by_user_id,
    PasswordResetLink.expires_at,
    PasswordResetLink.used_at,
    PasswordResetLink.revoked_at,
)


def _live() -> Any:
    return (
        PasswordResetLink.used_at.is_(None)
        & PasswordResetLink.revoked_at.is_(None)
        & (PasswordResetLink.expires_at > func.now())
    )


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


class ResetLinkRepository(TenantScopedRepository[PasswordResetLink]):
    model = PasswordResetLink

    async def revoke_open(self, user_id: uuid.UUID) -> int:
        """Revoke the member's open link (live or expired): frees the one-live-link index."""
        stmt = self._scoped_write(
            update(PasswordResetLink)
            .where(self._tenant_criterion(include_platform=False))
            .where(
                PasswordResetLink.user_id == user_id,
                PasswordResetLink.used_at.is_(None),
                PasswordResetLink.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
        )
        return _rowcount(await self.db.execute(stmt))

    async def create(
        self, *, user_id: uuid.UUID, token_hash: bytes, issued_by: uuid.UUID, ttl: timedelta
    ) -> ResetLinkRow:
        """A live link; `expires_at` is the database's `now() + ttl` (the 24-hour CHECK compares
        it with `created_at`, the same `now()`)."""
        stmt = (
            insert(PasswordResetLink)
            .values(
                tenant_id=self.tenant_id,
                user_id=user_id,
                token_hash=token_hash,
                issued_by_user_id=issued_by,
                expires_at=func.now() + ttl,
            )
            .returning(*_COLUMNS)
        )
        row = (await self.db.execute(stmt)).one()
        return ResetLinkRow(
            id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            issued_by_user_id=row.issued_by_user_id,
            expires_at=row.expires_at,
            used_at=row.used_at,
            revoked_at=row.revoked_at,
            live=True,
        )

    async def lock(self, link_id: uuid.UUID) -> ResetLinkRow | None:
        """The row `FOR UPDATE` (the redeem step), with whether it is still live."""
        stmt = (
            self._select()
            .with_only_columns(*_COLUMNS, _live().label("live"))
            .where(PasswordResetLink.id == link_id)
            .with_for_update(of=PasswordResetLink)
        )
        found = (await self.db.execute(stmt)).one_or_none()
        return None if found is None else ResetLinkRow(*found)

    async def mark_used(self, link_id: uuid.UUID) -> None:
        stmt = self._scoped_write(
            update(PasswordResetLink)
            .where(self._tenant_criterion(include_platform=False))
            .where(
                PasswordResetLink.id == link_id,
                PasswordResetLink.used_at.is_(None),
                PasswordResetLink.revoked_at.is_(None),
            )
            .values(used_at=func.now())
        )
        count = _rowcount(await self.db.execute(stmt))
        if count != 1:
            raise RowCountError(1, count)
