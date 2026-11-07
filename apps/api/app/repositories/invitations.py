"""A tenant's invitations (SI5, ADR-0029; DATABASE.md:287-297), scoped like every
`TenantScopedRepository`. The status is derived, never stored: `accepted`, `revoked`, `expired`
(`expires_at <= now()`), else `pending`. One pending row per email (`uq_invitations_pending`): a
re-invite revokes the open row first, expired or not.

The pre-tenant read by token hash is `UserRepository.invitation_by_hash` (identity.py, repo rule
R7); everything here runs after the tenant GUC is set."""

from __future__ import annotations

import builtins
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import Select, case, func, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from app.core.errors import RowCountError
from app.db.models.tenancy import Invitation, TenantMembership, User
from app.repositories.base import TenantScopedRepository

STATUSES: Final = ("pending", "accepted", "revoked", "expired")


@dataclass(frozen=True, slots=True)
class InviteRow:
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: str
    invited_by_user_id: uuid.UUID | None
    expires_at: datetime
    created_at: datetime
    accepted_at: datetime | None
    accepted_user_id: uuid.UUID | None
    revoked_at: datetime | None
    status: str


def _status() -> Any:
    return case(
        (Invitation.accepted_at.is_not(None), literal("accepted")),
        (Invitation.revoked_at.is_not(None), literal("revoked")),
        (Invitation.expires_at <= func.now(), literal("expired")),
        else_=literal("pending"),
    )


_COLUMNS: Final = (
    Invitation.id,
    Invitation.tenant_id,
    Invitation.email,
    Invitation.role,
    Invitation.invited_by_user_id,
    Invitation.expires_at,
    Invitation.created_at,
    Invitation.accepted_at,
    Invitation.accepted_user_id,
    Invitation.revoked_at,
)


def _row(row: Any) -> InviteRow:
    return InviteRow(*row)


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


class InvitationRepository(TenantScopedRepository[Invitation]):
    model = Invitation

    def _rows(self) -> Select[Any]:
        return self._select().with_only_columns(*_COLUMNS, _status().label("status"))

    def _open(self) -> Any:
        return (Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))

    async def create(
        self,
        *,
        email: str,
        role: str,
        token_hash: bytes,
        invited_by: uuid.UUID,
        ttl: timedelta,
    ) -> InviteRow:
        """A pending invite; `expires_at` is the database's `now() + ttl`."""
        stmt = (
            insert(Invitation)
            .values(
                tenant_id=self.tenant_id,
                email=email,
                role=role,
                token_hash=token_hash,
                invited_by_user_id=invited_by,
                expires_at=func.now() + ttl,
            )
            .returning(*_COLUMNS, literal("pending").label("status"))
        )
        return _row((await self.db.execute(stmt)).one())

    async def revoke_open_for_email(self, email: str) -> int:
        """Revoke the open row of `email` (pending or expired): frees `uq_invitations_pending`."""
        stmt = self._scoped_write(
            update(Invitation)
            .where(self._tenant_criterion(include_platform=False))
            .where(Invitation.email == email, *self._open())
            .values(revoked_at=func.now())
        )
        return _rowcount(await self.db.execute(stmt))

    async def open_count(self) -> int:
        """Pending invites that have not expired."""
        stmt = (
            self._select()
            .with_only_columns(func.count())
            .where(*self._open(), Invitation.expires_at > func.now())
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def page(
        self,
        *,
        status: str | None,
        limit: int,
        after: tuple[datetime, uuid.UUID] | None,
    ) -> builtins.list[InviteRow]:
        """Up to `limit` rows, newest first on `(created_at, id)`, after the cursor."""
        stmt = self._rows()
        if status is not None:
            stmt = stmt.where(_status() == status)
        if after is not None:
            at, row_id = after
            bound = tuple_(
                literal(at, Invitation.created_at.type), literal(row_id, Invitation.id.type)
            )
            stmt = stmt.where(tuple_(Invitation.created_at, Invitation.id) < bound)
        stmt = stmt.order_by(Invitation.created_at.desc(), Invitation.id.desc()).limit(limit)
        return [_row(r) for r in (await self.db.execute(stmt)).all()]

    async def get(self, invite_id: uuid.UUID) -> InviteRow | None:
        stmt = self._rows().where(Invitation.id == invite_id)
        found = (await self.db.execute(stmt)).one_or_none()
        return None if found is None else _row(found)

    async def lock(self, invite_id: uuid.UUID) -> InviteRow | None:
        """The row `FOR UPDATE` (the accept and revoke steps), with its derived status."""
        stmt = self._rows().where(Invitation.id == invite_id).with_for_update(of=Invitation)
        found = (await self.db.execute(stmt)).one_or_none()
        return None if found is None else _row(found)

    async def mark_accepted(self, invite_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self._close(invite_id, accepted_at=func.now(), accepted_user_id=user_id)

    async def mark_revoked(self, invite_id: uuid.UUID) -> None:
        await self._close(invite_id, revoked_at=func.now())

    async def _close(self, invite_id: uuid.UUID, **values: Any) -> None:
        stmt = self._scoped_write(
            update(Invitation)
            .where(self._tenant_criterion(include_platform=False))
            .where(Invitation.id == invite_id, *self._open())
            .values(**values)
        )
        count = _rowcount(await self.db.execute(stmt))
        if count != 1:
            raise RowCountError(1, count)

    async def email_is_member(self, email: str) -> bool:
        """Whether `email` holds a membership here, active or not (users_teammates)."""
        stmt = self._scoped_read(
            select(TenantMembership.user_id)
            .join(User, User.id == TenantMembership.user_id)
            .where(
                self._criterion_for(TenantMembership),
                func.lower(User.email) == email.strip().lower(),
            )
            .limit(1)
        )
        return (await self.db.execute(stmt)).first() is not None
