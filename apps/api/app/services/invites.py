"""Team invites by copied link (SI5, ADR-0029; DATABASE.md:287-297).

- `create_invite`: an owner or admin invites an email with a role. Only an owner invites an owner
  (the `members._owner_rule` mirror). An email with a membership here (active or not) is 422 at
  `email`; an open invite for the email is revoked and replaced in the same transaction; at most
  `MAX_PENDING_INVITES` pending. The token is 32 random bytes (43 base64url characters), stored
  as its sha256 only and returned once inside the link's fragment.
- `list_invites`, `revoke_invite`: the tenant's invites, newest first; revoking an accepted invite
  is 409, an owner invite only by an owner, a replay answers the stored row.
- `lookup`, `accept_password`, `accept_google`, `join`: the public steps and the signed-in join.
  Every refusal of a well-formed token is one `NotFoundError("token")` (`_live`).

Audit rows carry the role only (`audit_allowlist('invitation')`), never the email or the token.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from mkos_schemas.codes import ErrorCode

from app.core.config import get_settings
from app.core.errors import ConflictError, ForbiddenError, InputError, NotFoundError
from app.core.rbac import Principal
from app.repositories.identity import AuditRepository
from app.repositories.invitations import InvitationRepository, InviteRow
from app.repositories.transactions import unit_of_work

__all__ = [
    "INVITE_TTL",
    "MAX_PENDING_INVITES",
    "InvitePage",
    "InviteRow",
    "create_invite",
    "list_invites",
    "revoke_invite",
]

INVITE_TTL: Final = timedelta(days=7)
MAX_PENDING_INVITES: Final = 50
TOKEN_BYTES: Final = 32


@dataclass(frozen=True, slots=True)
class InvitePage:
    items: list[InviteRow]
    has_more: bool


def token_hash(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("ascii")).digest()


def _owner_rule(principal: Principal, role: str) -> None:
    if role == "owner" and principal.role != "owner":
        raise ForbiddenError("only an owner invites an owner")


def _role(role: str) -> str:
    return json.dumps({"role": role})


async def create_invite(principal: Principal, *, email: str, role: str) -> tuple[InviteRow, str]:
    """The new pending invite and its link (the only place the raw token exists)."""
    _owner_rule(principal, role)
    tenant = principal.tenant_id
    async with unit_of_work(tenant, user_id=principal.user_id) as session:
        invites = InvitationRepository(session, tenant)
        if await invites.email_is_member(email):
            raise InputError(ErrorCode.VALIDATION_FAILED, "email")
        await invites.revoke_open_for_email(email)
        if await invites.open_count() >= MAX_PENDING_INVITES:
            raise InputError(ErrorCode.VALIDATION_FAILED, "email")
        raw = secrets.token_urlsafe(TOKEN_BYTES)
        row = await invites.create(
            email=email,
            role=role,
            token_hash=token_hash(raw),
            invited_by=principal.user_id,
            ttl=INVITE_TTL,
        )
        await AuditRepository(session).write(
            tenant_id=tenant,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="invitation.created",
            entity_type="invitation",
            entity_id=row.id,
            after=_role(role),
        )
    return row, f"{get_settings().dashboard_base_url}/invite#token={raw}"


async def list_invites(
    principal: Principal,
    *,
    status: str | None,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None,
) -> InvitePage:
    async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
        rows = await InvitationRepository(session, principal.tenant_id).page(
            status=status, limit=limit + 1, after=after
        )
    return InvitePage(items=rows[:limit], has_more=len(rows) > limit)


async def revoke_invite(principal: Principal, invite_id: uuid.UUID) -> InviteRow:
    tenant = principal.tenant_id
    async with unit_of_work(tenant, user_id=principal.user_id) as session:
        invites = InvitationRepository(session, tenant)
        row = await invites.lock(invite_id)
        if row is None:
            raise NotFoundError("invite_id")
        _owner_rule(principal, row.role)
        if row.status == "accepted":
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "invitations_status")
        if row.status == "revoked":
            return row
        await invites.mark_revoked(invite_id)
        await AuditRepository(session).write(
            tenant_id=tenant,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="invitation.revoked",
            entity_type="invitation",
            entity_id=invite_id,
            after=_role(row.role),
        )
        revoked = await invites.get(invite_id)
    if revoked is None:
        raise RuntimeError("a revoked invite is not readable")
    return revoked
