"""Members (CODE-PLAN S10 C12; PLAN Decision 5; DATABASE.md:246-258).

- `list_members`: the caller's tenant's members, active or not, in keyset pages.
- `patch_member`: change one member's `role` and/or `is_active`.
  - **Owner rule:** a caller whose (row) role is not `owner` may neither touch a member whose
    current role is `owner` nor grant `owner`, whatever the target (itself included) and whether
    it is active: an admin can never create, remove, demote, deactivate or reactivate an owner.
  - **Self rule:** a non-owner never changes its own role (T88, PLAN Decision 9: an admin is not
    an approver); replaying its current role is a no-op, and self-deactivation stays allowed.
  - The last-owner trigger runs at the statement (`SET CONSTRAINTS ... IMMEDIATE`), so leaving a
    tenant with no active owner is `OWN01` -> 409 `LAST_OWNER_REQUIRED` from this call.
  - Deactivation revokes that membership's refresh tokens (`membership_deactivated`).
  - Audit `membership.role_changed`, `.deactivated`, `.reactivated`, only when a value changes,
    so a replayed body writes nothing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.errors import ForbiddenError, NotFoundError
from app.core.rbac import Principal
from app.repositories.identity import AuditRepository, RefreshTokenRepository
from app.repositories.members import MemberRow, MembersRepository
from app.repositories.transactions import unit_of_work

__all__ = ["MemberPage", "MemberRow", "list_members", "patch_member"]


@dataclass(frozen=True, slots=True)
class MemberPage:
    items: list[MemberRow]
    has_more: bool


async def list_members(
    principal: Principal, *, limit: int, after: tuple[datetime, uuid.UUID] | None
) -> MemberPage:
    async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
        rows = await MembersRepository(session, principal.tenant_id).page(
            limit=limit + 1, after=after
        )
    return MemberPage(items=rows[:limit], has_more=len(rows) > limit)


def _owner_rule(principal: Principal, target: MemberRow, role: str | None) -> None:
    if principal.role != "owner" and (target.role == "owner" or role == "owner"):
        raise ForbiddenError("only an owner grants or removes owner")


def _self_rule(principal: Principal, target: MemberRow, role: str | None) -> None:
    # T88 / PLAN Decision 9: an admin is not an approver, so a non-owner never changes its own
    # role (an admin -> reviewer/operator would gain listing:approve or listing:publish).
    # Replaying the current role stays a no-op 200; self-deactivation stays allowed.
    if (
        principal.role != "owner"
        and target.user_id == principal.user_id
        and role is not None
        and role != target.role
    ):
        raise ForbiddenError("only an owner changes its own role")


async def patch_member(
    principal: Principal,
    user_id: uuid.UUID,
    *,
    role: str | None,
    is_active: bool | None,
) -> MemberRow:
    tenant = principal.tenant_id
    async with unit_of_work(tenant, user_id=principal.user_id) as session:
        members = MembersRepository(session, tenant)
        target = await members.get_for_update(user_id)
        if target is None:
            raise NotFoundError("member")
        _owner_rule(principal, target, role)
        _self_rule(principal, target, role)
        new_role = role if role is not None and role != target.role else None
        new_active = is_active if is_active is not None and is_active != target.is_active else None
        if new_role is None and new_active is None:
            return target
        values: dict[str, object] = {}
        if new_role is not None:
            values["role"] = new_role
        if new_active is not None:
            values["is_active"] = new_active
            values["deactivated_at"] = None if new_active else datetime.now(UTC)
        await members.check_owners_at_each_statement()
        await members.update_one(target.membership_id, **values)
        if new_active is False:
            await RefreshTokenRepository(session).revoke_membership(
                tenant, user_id, "membership_deactivated"
            )
        audit = AuditRepository(session)
        if new_role is not None:
            await audit.write(
                tenant_id=tenant,
                actor_type="user",
                actor_user_id=principal.user_id,
                action="membership.role_changed",
                entity_type="membership",
                entity_id=target.membership_id,
                before=json.dumps({"role": target.role}),
                after=json.dumps({"role": new_role}),
            )
        if new_active is not None:
            await audit.write(
                tenant_id=tenant,
                actor_type="user",
                actor_user_id=principal.user_id,
                action="membership.reactivated" if new_active else "membership.deactivated",
                entity_type="membership",
                entity_id=target.membership_id,
                before=json.dumps({"is_active": target.is_active}),
                after=json.dumps({"is_active": new_active}),
            )
    return MemberRow(
        membership_id=target.membership_id,
        user_id=target.user_id,
        email=target.email,
        display_name=target.display_name,
        role=new_role or target.role,
        is_active=target.is_active if new_active is None else new_active,
        created_at=target.created_at,
    )
