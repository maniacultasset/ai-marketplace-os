"""A tenant's members (CODE-PLAN S10 C12; PLAN Decision 5): memberships joined to their users,
scoped to the repository's tenant like every `TenantScopedRepository` (the users columns are
visible to app_rw through the `users_teammates` policy, DATABASE.md:244)."""

from __future__ import annotations

import builtins
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text, tuple_

from app.db.models.tenancy import TenantMembership, User
from app.repositories.base import TenantScopedRepository

_OWNER_GUARD_NOW: Final = text("SET CONSTRAINTS last_owner_guard IMMEDIATE")


@dataclass(frozen=True, slots=True)
class MemberRow:
    membership_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    is_active: bool
    created_at: datetime


def _row(row: Any) -> MemberRow:
    membership, email, display_name = row
    return MemberRow(
        membership_id=membership.id,
        user_id=membership.user_id,
        email=email,
        display_name=display_name,
        role=membership.role,
        is_active=membership.is_active,
        created_at=membership.created_at,
    )


class MembersRepository(TenantScopedRepository[TenantMembership]):
    model = TenantMembership

    def _members(self) -> Any:
        return (
            self._select()
            .add_columns(User.email, User.display_name)
            .join(User, User.id == TenantMembership.user_id)
        )

    async def page(
        self, *, limit: int, after: tuple[datetime, uuid.UUID] | None
    ) -> builtins.list[MemberRow]:
        """Up to `limit` members in keyset order `(created_at, id)`, after the cursor."""
        stmt = self._members()
        if after is not None:
            stmt = stmt.where(tuple_(TenantMembership.created_at, TenantMembership.id) > after)
        stmt = stmt.order_by(TenantMembership.created_at, TenantMembership.id).limit(limit)
        return [_row(r) for r in (await self.db.execute(stmt)).all()]

    async def get_for_update(self, user_id: uuid.UUID) -> MemberRow | None:
        """The member, its membership row locked `FOR NO KEY UPDATE` (the lock its UPDATE takes
        anyway). Not `FOR UPDATE`: that also blocks the `FOR KEY SHARE` an audit row's actor FK
        takes on a membership, so two owners demoting each other would deadlock against the
        last-owner trigger's advisory lock instead of one getting 409."""
        stmt = (
            self._members()
            .where(TenantMembership.user_id == user_id)
            .with_for_update(of=TenantMembership, key_share=True)
        )
        found = (await self.db.execute(stmt)).one_or_none()
        return None if found is None else _row(found)

    async def role_for_share(self, user_id: uuid.UUID) -> tuple[str, bool] | None:
        """The member's `(role, is_active)`, its membership row read `FOR SHARE` (S19: the
        approver's membership, DATABASE.md §7.1), so a concurrent demotion or deactivation waits
        for this transaction or is seen by it; None for a non-member."""
        stmt = (
            self._select()
            .where(TenantMembership.user_id == user_id)
            .with_only_columns(TenantMembership.role, TenantMembership.is_active)
            .with_for_update(read=True)
        )
        found = (await self.db.execute(stmt)).one_or_none()
        return None if found is None else (found.role, found.is_active)

    async def check_owners_at_each_statement(self) -> None:
        """Run the deferred last-owner trigger at the statement (OWN01 there, not at COMMIT)."""
        await self.db.execute(_OWNER_GUARD_NOW)
