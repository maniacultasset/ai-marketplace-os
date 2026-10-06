"""Authentication (BACKEND-ARCHITECTURE.md §9.2; DATABASE.md:306-312; CODE-PLAN S10 Q10).

`probe_principal` turns verified access claims into the `Principal`: one membership probe inside
the token's tenant, and the role from the row (a demoted user's old token carries the old role).
"""

from __future__ import annotations

import ipaddress
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any, Final
from uuid import UUID

from mkos_schemas.auth import JWTClaims

from app.core.config import get_settings
from app.core.passwords import hash_password, verify_password
from app.core.rbac import Principal
from app.core.security import encode, issue_claims, token_hash
from app.repositories.identity import (
    AuditRepository,
    MembershipRepository,
    MembershipRow,
    RefreshTokenRepository,
    UserRepository,
)
from app.repositories.transactions import identity_session, set_gucs, unit_of_work


async def probe_principal(claims: JWTClaims) -> Principal | None:
    """The caller, or None when the membership is inactive, the user inactive or the tenant
    neither `trial` nor `active`."""
    async with unit_of_work(claims.tid, user_id=claims.sub) as session:
        row = await MembershipRepository(session).probe(claims.tid, claims.sub)
    if row is None:
        return None
    return Principal(
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        role=row.role,  # type: ignore[arg-type]
        email=row.email,
        jti=claims.jti,
    )


# ---- login (CODE-PLAN S10 C8; DATABASE.md:308 GUC order) ----

USER_AGENT_MAX: Final = 512


@dataclass(frozen=True, slots=True)
class Membership:
    tenant_id: UUID
    tenant_name: str
    role: str


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    memberships: list[Membership]


@cache
def _dummy_hash() -> str:
    """A real argon2id hash of a random secret: an unknown email still costs one verify."""
    return hash_password(secrets.token_urlsafe(32))


def _inet(ip: str | None) -> str | None:
    """The address for an `inet` column, or None when the limiter's value is not an IP."""
    if ip is None:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def _agent(user_agent: str | None) -> str | None:
    return user_agent[:USER_AGENT_MAX] if user_agent else None


async def _issue(
    session: Any,
    *,
    user_id: UUID,
    tenant_id: UUID,
    role: str,
    email: str,
    family_id: UUID,
    ip: str | None,
    user_agent: str | None,
) -> tuple[str, str, UUID]:
    """A signed access + refresh pair for one tenant, the refresh row stored by hash. The
    caller has set `app.tenant_id` = `tenant_id` and `app.user_id` = `user_id` (Q10)."""
    access = issue_claims(sub=user_id, tid=tenant_id, role=role, email=email, typ="access")  # type: ignore[arg-type]
    refresh = issue_claims(sub=user_id, tid=tenant_id, role=role, email=email, typ="refresh")  # type: ignore[arg-type]
    raw_refresh = encode(refresh)
    row_id = await RefreshTokenRepository(session).insert(
        tenant_id=tenant_id,
        user_id=user_id,
        family_id=family_id,
        token_hash=token_hash(raw_refresh),
        expires_at=datetime.fromtimestamp(refresh.exp, UTC),
        ip=_inet(ip),
        user_agent=_agent(user_agent),
    )
    return encode(access), raw_refresh, row_id


def _access_ttl() -> int:
    return get_settings().access_token_ttl_minutes * 60


async def login(
    email: str, password: str, *, ip: str | None, user_agent: str | None
) -> TokenPair | None:
    """Email + password in; a token pair for the most recently used live tenant out, or None
    (the same None for an unknown email, a wrong password, an inactive user and no live
    membership: one argon2 verify each). A failure writes `auth.login_failed` and commits it
    before the caller answers 401 (Q10 (4))."""
    async with identity_session() as session:
        await set_gucs(session, login_email=email)
        user = await UserRepository(session).by_email(email)
        stored = user.hashed_password if user is not None and user.hashed_password else None
        verified = verify_password(password, stored or _dummy_hash())
        memberships: list[MembershipRow] = []
        reason = "unknown_email"
        if user is not None:
            reason = "wrong_password"
            if verified and stored is not None:
                reason = "inactive_user"
                if user.is_active:
                    await set_gucs(session, user_id=user.id)
                    memberships = await MembershipRepository(session).list_active_for_user(user.id)
                    reason = "no_active_membership"
        if not memberships:
            await AuditRepository(session).write(
                tenant_id=None,
                actor_type="system",
                actor_user_id=None,
                action="auth.login_failed",
                entity_type="user",
                entity_id=user.id if user is not None else None,
                reason=reason,
                ip=_inet(ip),
            )
            return None
        assert user is not None  # noqa: S101 - memberships are read only for a verified user
        chosen = memberships[0]
        await set_gucs(session, tenant_id=chosen.tenant_id)
        await MembershipRepository(session).touch_last_used(chosen.tenant_id, user.id)
        await UserRepository(session).touch_login(user.id)
        access, refresh, _ = await _issue(
            session,
            user_id=user.id,
            tenant_id=chosen.tenant_id,
            role=chosen.role,
            email=user.email,
            family_id=uuid.uuid4(),
            ip=ip,
            user_agent=user_agent,
        )
        await AuditRepository(session).write(
            tenant_id=chosen.tenant_id,
            actor_type="user",
            actor_user_id=user.id,
            action="auth.login",
            entity_type="user",
            entity_id=user.id,
            ip=_inet(ip),
        )
    return TokenPair(access, refresh, _access_ttl(), [_membership(m) for m in memberships])


def _membership(row: MembershipRow) -> Membership:
    return Membership(row.tenant_id, row.tenant_name, row.role)
