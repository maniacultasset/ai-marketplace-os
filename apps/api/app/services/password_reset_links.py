"""Copied password reset links (SI5, ADR-0029 §3; PLAN D8, D9).

An owner or admin issues a 24-hour, single-use link for a member; the member sets a new password
with it. The link resets a global password, and an admin could reset a reviewer and act as them,
so a link exists only for a member who:
- holds a membership in this tenant only (any other membership, whatever its status, refuses),
  and is active here;
- is not the issuer, not a platform admin, and has a password (an erased or Google-only account
  has none);
- has a confirmed TOTP factor, and only while `MFA_ENFORCEMENT=all`: under `off` a password alone
  signs in, so the factor would not stop a takeover and every link is refused;
- is not an owner, unless the issuer is an owner.

Everyone else is 403 "Kuinn support resets this account" (the staff CLI). Redemption re-checks
all of it, and that the issuer is still an active owner or admin here (an owner when the target is
an owner now); anything else is the generic 404. A redeem sets the password (the DPP rules and
none of the last 10), ends every refresh family and trusted browser and every open challenge, and
leaves the MFA factors and the lockout state alone.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.core.config import get_settings
from app.core.errors import ForbiddenError, InputError, NotFoundError
from app.core.rbac import Principal
from app.repositories.identity import (
    AuditRepository,
    MembershipRepository,
    RefreshTokenRepository,
    UserRepository,
)
from app.repositories.members import MembersRepository
from app.repositories.mfa import ChallengeRepository, MfaFactorRepository
from app.repositories.password_reset_links import ResetLinkRepository
from app.repositories.transactions import identity_session, set_gucs, unit_of_work
from app.services import auth
from app.services import trusted_devices as trusted

__all__ = ["RESET_TTL", "NotEligibleError", "issue", "redeem"]

RESET_TTL: Final = timedelta(hours=24)
TOKEN_BYTES: Final = 32
ISSUERS: Final = ("owner", "admin")
NOT_ELIGIBLE_MESSAGE: Final = "Kuinn support resets this account."


class NotEligibleError(ForbiddenError):
    """The member cannot get a reset link (the staff CLI resets them)."""


def _hash(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("ascii")).digest()


def _gone() -> NotFoundError:
    return NotFoundError("token")


async def _eligible(tenant_id: uuid.UUID, target_id: uuid.UUID, *, issuer_role: str | None) -> bool:
    """Read as the target (`memberships_own`, `users_self`, `umf_self`) in its own session."""
    if not auth.enforcing():
        return False
    async with identity_session() as session:
        await set_gucs(session, user_id=target_id)
        memberships = await MembershipRepository(session).every_for_user(target_id)
        if [m[0] for m in memberships] != [tenant_id]:
            return False
        _, role, active = memberships[0]
        if not active:
            return False
        users = UserRepository(session)
        user = await users.self_row(target_id)
        if user is None or not user.is_active or not user.hashed_password:
            return False
        if user.is_platform_admin:
            return False
        if not await MfaFactorRepository(session).has_active(target_id):
            return False
    return issuer_role is None or role != "owner" or issuer_role == "owner"


async def issue(principal: Principal, user_id: uuid.UUID) -> tuple[str, datetime]:
    """The link for a member of the caller's tenant, and when it expires."""
    if user_id == principal.user_id:
        raise InputError(ErrorCode.VALIDATION_FAILED, "user_id")
    tenant = principal.tenant_id
    async with unit_of_work(tenant, user_id=principal.user_id) as session:
        target = await MembersRepository(session, tenant).role_for_share(user_id)
    if target is None:
        raise NotFoundError("user_id")
    if target[0] == "owner" and principal.role != "owner":
        raise ForbiddenError("only an owner resets an owner")
    if not await _eligible(tenant, user_id, issuer_role=principal.role):
        raise NotEligibleError(NOT_ELIGIBLE_MESSAGE)
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    async with unit_of_work(tenant, user_id=principal.user_id) as session:
        links = ResetLinkRepository(session, tenant)
        await links.lock_member(user_id)  # a double-clicked reset queues here (tester R2)
        await links.revoke_open(user_id)
        link = await links.create(
            user_id=user_id, token_hash=_hash(raw), issued_by=principal.user_id, ttl=RESET_TTL
        )
        await AuditRepository(session).write(
            tenant_id=tenant,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="user.password_reset_issued",
            entity_type="user",
            entity_id=user_id,
        )
    return f"{get_settings().dashboard_base_url}/reset-password#token={raw}", link.expires_at


async def _issuer_ok(session: Any, tenant: uuid.UUID, issuer: uuid.UUID, target_role: str) -> bool:
    found = await MembersRepository(session, tenant).role_for_share(issuer)
    if found is None:
        return False
    role, active = found
    return active and role in ISSUERS and (target_role != "owner" or role == "owner")


async def redeem(raw: str, new_password: str, *, ip: str | None) -> auth.Refusal | None:
    """Set the new password with a live link; a `policy` or `reused` refusal keeps it live."""
    async with identity_session() as session:
        found_link = await UserRepository(session).reset_link_by_hash(_hash(raw))
    if found_link is None:
        raise _gone()
    tenant, target = found_link.tenant_id, found_link.user_id
    if not await _eligible(tenant, target, issuer_role=None):
        raise _gone()
    async with identity_session() as session:
        await set_gucs(session, tenant_id=tenant, user_id=target)
        link = await ResetLinkRepository(session, tenant).lock(found_link.id)
        if link is None or not link.live:
            raise _gone()
        member = await MembersRepository(session, tenant).role_for_share(target)
        if member is None or not member[1]:
            raise _gone()
        if not await _issuer_ok(session, tenant, link.issued_by_user_id, member[0]):
            raise _gone()
        user = await UserRepository(session).self_row(target)
        if user is None:
            raise _gone()
        refusal = await auth.new_password_refusal(session, user, new_password)
        if refusal is not None:
            return refusal
        await auth.store_password(session, user, new_password)
        await RefreshTokenRepository(session).revoke_user_everywhere(
            target, keep_family=None, reason="password_changed"
        )
        await trusted.forget_all(
            session, target, "password_changed", ip=auth.inet(ip), tenant_id=tenant
        )
        await ChallengeRepository(session).delete_open(target)
        await ResetLinkRepository(session, tenant).mark_used(link.id)
        await AuditRepository(session).write(
            tenant_id=tenant,
            actor_type="user",
            actor_user_id=target,
            action="user.password_reset",
            entity_type="user",
            entity_id=target,
            ip=auth.inet(ip),
        )
    return None
