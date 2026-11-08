"""Team invites by copied link (SI5, ADR-0029; DATABASE.md:287-297).

- `create_invite`: an owner or admin invites an email with a role. Only an owner invites an owner
  or a reviewer, the roles that approve (V1 hardening #12: an admin is not an approver, T88, so it
  never makes one). An email with a membership here (active or not) is 422 at
  `email`; an open invite for the email is revoked and replaced in the same transaction; at most
  `MAX_PENDING_INVITES` pending. The token is 32 random bytes (43 base64url characters), stored
  as its sha256 only and returned once inside the link's fragment.
- `list_invites`, `revoke_invite`: the tenant's invites, newest first; revoking an accepted invite
  is 409, an owner invite only by an owner, a replay answers the stored row.
- `lookup`, `accept_password`, `accept_google`, `join`: the public steps and the signed-in join.
  Every refusal of a well-formed token is one `NotFoundError("token")` (`_live`). An email whose
  account can sign in (active, with a live membership) is `existing` and joins signed in; any
  other email accepts. An account that cannot sign in yet (a pending or rejected applicant,
  deactivated from every brand, or its only brands suspended or offboarded) is `own_password`
  when it has a password and accepts with it under the lockout gate
  (`signup.verify_existing_user`), never a new one; with no account, or a Google-only one, `new`.
  An account `accept_password` creates is `email_unverified` (nothing proved the link's holder
  owns the address): it cannot approve until a Google sign-in with that verified email clears it.

Audit rows carry the role only (`audit_allowlist('invitation')`), never the email or the token.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from mkos_schemas.codes import ErrorCode

from app.core import password_policy
from app.core.config import get_settings
from app.core.errors import ConflictError, ForbiddenError, InputError, NotFoundError
from app.core.passwords import hash_password, off_loop
from app.core.rbac import Principal
from app.repositories.identity import (
    AuditRepository,
    InvitationLookup,
    MembershipRepository,
    UserRepository,
    UserRow,
)
from app.repositories.invitations import InvitationRepository, InviteRow
from app.repositories.members import MembersRepository
from app.repositories.tenants import TenantProfileRepository
from app.repositories.transactions import identity_session, set_gucs, unit_of_work
from app.services import google_signin
from app.services.signup import verify_existing_user

__all__ = [
    "INVITE_TTL",
    "MAX_PENDING_INVITES",
    "Accepted",
    "AccountRefusedError",
    "InvitePage",
    "InviteRow",
    "Joined",
    "Preview",
    "accept_google",
    "accept_password",
    "create_invite",
    "join",
    "list_invites",
    "lookup",
    "revoke_invite",
]

INVITE_TTL: Final = timedelta(days=7)
MAX_PENDING_INVITES: Final = 50
TOKEN_BYTES: Final = 32
Account = Literal["new", "existing", "own_password"]
LIVE_TENANT: Final = ("trial", "active")
INVITERS: Final = ("owner", "admin")
# V1 hardening #12: the roles that approve; only an owner invites them
OWNER_ONLY_ROLES: Final = frozenset({"owner", "reviewer"})


@dataclass(frozen=True, slots=True)
class InvitePage:
    items: list[InviteRow]
    has_more: bool


def token_hash(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("ascii")).digest()


def _owner_rule(principal: Principal, role: str) -> None:
    if role in OWNER_ONLY_ROLES and principal.role != "owner":
        raise ForbiddenError("only an owner invites an owner or a reviewer")


def _role(role: str) -> str:
    return json.dumps({"role": role})


async def create_invite(principal: Principal, *, email: str, role: str) -> tuple[InviteRow, str]:
    """The new pending invite and its link (the only place the raw token exists)."""
    _owner_rule(principal, role)
    tenant = principal.tenant_id
    async with unit_of_work(tenant, user_id=principal.user_id) as session:
        invites = InvitationRepository(session, tenant)
        await invites.lock_email(email)  # first: a double-clicked Invite queues here (tester R1)
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


# ---- the public steps and the signed-in join (T6) ----


@dataclass(frozen=True, slots=True)
class Preview:
    tenant_name: str
    role: str
    email: str
    expires_at: datetime
    account: Account


@dataclass(frozen=True, slots=True)
class Accepted:
    email: str
    tenant_name: str


@dataclass(frozen=True, slots=True)
class Joined:
    tenant_id: uuid.UUID
    role: str


@dataclass(frozen=True, slots=True)
class LiveInvite:
    """A pending invite, locked `FOR UPDATE` under its tenant, and its tenant's name."""

    row: InviteRow
    tenant_name: str


class AccountRefusedError(Exception):
    """The existing account's own password did not verify (wrong, locked or inactive): the
    route's 401. Raised after the session commits, so a wrong password stays counted."""


def _gone() -> NotFoundError:
    """The one refusal of a well-formed token: every body is the same (no oracle)."""
    return NotFoundError("token")


async def _find(session: Any, raw: str) -> InvitationLookup:
    found = await UserRepository(session).invitation_by_hash(token_hash(raw))
    if found is None:
        raise _gone()
    return found


async def _live(session: Any, found: InvitationLookup) -> LiveInvite:
    """The second read (CODE-PLAN §2.2): under the invite's tenant, the row `FOR UPDATE`, then
    the tenant and the inviter. Anything but a live invite is the generic refusal:
    - accepted, revoked or expired (`expires_at <= now()`);
    - the tenant not `trial` or `active`;
    - the inviter no longer an active owner or admin here, or not an owner for an owner invite."""
    tenant = found.tenant_id
    await set_gucs(session, tenant_id=tenant)
    row = await InvitationRepository(session, tenant).lock(found.id)
    if row is None or row.status != "pending" or row.invited_by_user_id is None:
        raise _gone()
    profile = await TenantProfileRepository(session, tenant).get_profile()
    if profile is None or profile.status not in LIVE_TENANT:
        raise _gone()
    inviter = await MembersRepository(session, tenant).role_for_share(row.invited_by_user_id)
    if inviter is None:
        raise _gone()
    inviter_role, inviter_active = inviter
    if not inviter_active or inviter_role not in INVITERS:
        raise _gone()
    if row.role == "owner" and inviter_role != "owner":
        raise _gone()
    return LiveInvite(row=row, tenant_name=profile.name)


async def _can_sign_in(session: Any, user: UserRow) -> bool:
    """The login's own test (`auth.login`): an active user with an active membership in a live
    tenant. Sets `app.user_id` = the user (memberships_own)."""
    if not user.is_active:
        return False
    await set_gucs(session, user_id=user.id)
    return bool(await MembershipRepository(session).list_active_for_user(user.id))


async def _account(session: Any, user: UserRow | None) -> Account:
    """`existing`: the account can sign in (sign in, then join). `own_password`: it cannot sign
    in yet and has a password, which accept verifies. `new`: no account, or a Google-only one
    that cannot sign in (KNOWN-ISSUES: no way in yet)."""
    if user is None:
        return "new"
    if await _can_sign_in(session, user):
        return "existing"
    return "new" if user.hashed_password is None else "own_password"


async def lookup(raw: str) -> Preview:
    """What the link is for, and what its email's account means for the next step
    (`_account`: `existing`, `own_password` or `new`)."""
    async with identity_session() as session:
        live = await _live(session, await _find(session, raw))
        await set_gucs(session, login_email=live.row.email)
        account = await _account(session, await UserRepository(session).by_email(live.row.email))
    return Preview(
        tenant_name=live.tenant_name,
        role=live.row.role,
        email=live.row.email,
        expires_at=live.row.expires_at,
        account=account,
    )


async def _accept(session: Any, live: LiveInvite, user_id: uuid.UUID) -> None:
    """The membership in the invite's tenant, the invite accepted, and the audit row."""
    row = live.row
    await set_gucs(session, user_id=user_id)
    await MembersRepository(session, row.tenant_id).insert(
        user_id=user_id, role=row.role, invited_by=row.invited_by_user_id
    )
    await InvitationRepository(session, row.tenant_id).mark_accepted(row.id, user_id)
    await AuditRepository(session).write(
        tenant_id=row.tenant_id,
        actor_type="user",
        actor_user_id=user_id,
        action="invitation.accepted",
        entity_type="invitation",
        entity_id=row.id,
        after=_role(row.role),
    )


async def _join_existing(
    session: Any, live: LiveInvite, user: UserRow, password: str, *, ip: str | None
) -> bool:
    """CR1: an existing account joins only when it cannot sign in (else 409: sign in, then
    join) and only with its own password under the lockout gate. False for a wrong password, a
    locked or an inactive account (counted by the gate); a membership here already is 409."""
    if await _can_sign_in(session, user):
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "invitations_account")
    if await MembersRepository(session, live.row.tenant_id).role_for_share(user.id) is not None:
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "invitations_member")
    if not await verify_existing_user(session, user, password, ip=ip):
        return False
    await _accept(session, live, user.id)
    return True


async def accept_password(
    raw: str, *, password: str, display_name: str | None, ip: str | None = None
) -> Accepted:
    """The invite accepted with a password, in one transaction. The DPP rules use the invite's
    email (`PolicyError`). A new email gets a new account and its membership. An existing
    account that can sign in is 409 (sign in, then join); one that cannot joins with its own
    password (`_join_existing`; `display_name` is ignored), and a wrong one is `AccountRefusedError`
    (401), raised after the session commits so the failure stays counted."""
    refused = False
    async with identity_session() as session:
        found = await _find(session, raw)
        breaches = password_policy.check(password, email=found.email, display_name=display_name)
        if breaches:
            raise password_policy.PolicyError(breaches)
        live = await _live(session, found)
        email = live.row.email
        await set_gucs(session, login_email=email)
        users = UserRepository(session)
        if not await users.lock_login_email(email):
            raise _gone()
        user = await users.by_email(email)
        if user is not None:
            refused = not await _join_existing(session, live, user, password, ip=ip)
        else:
            user_id = uuid.uuid4()
            created = await users.insert_signup_user(
                user_id=user_id,
                email=email,
                password_hash=await off_loop(hash_password, password),
                display_name=display_name,
                email_unverified=True,  # V1 hardening #12: a copied link proves no address
            )
            if not created:  # a concurrent account for the email won `uq_users_email_lower`
                raise _gone()
            await _accept(session, live, user_id)
    if refused:
        raise AccountRefusedError
    return Accepted(email=email, tenant_name=live.tenant_name)


async def join(principal: Principal, raw: str) -> Joined:
    """The signed-in caller joins the inviting tenant when the invite names its own email
    (another email is the generic 404). A membership there already, active or not, is 409."""
    async with identity_session() as session:
        live = await _live(session, await _find(session, raw))
        row = live.row
        if row.email != principal.email.strip().lower():
            raise _gone()
        members = MembersRepository(session, row.tenant_id)
        if await members.role_for_share(principal.user_id) is not None:
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "invitations_member")
        await _accept(session, live, principal.user_id)
    return Joined(tenant_id=row.tenant_id, role=row.role)


# ---- accept with Google (T7, PLAN D7) ----


async def accept_google(
    raw: str, *, handle: str, binding: str, display_name: str | None
) -> Accepted:
    """A new Google-only account (no password, the Google `sub`, a verified email) for the
    invite's email, from a `needs_details` handle of `auth_google_complete`. The Google email
    must be the invite's (422 at `google`); a taken email or Google account is 409. One
    transaction: any refusal leaves the handle unconsumed and writes nothing. Raises
    `google_signin.GoogleDisabled` (501) or `FlowInvalid` (401) for the handle."""
    async with identity_session() as session:
        identity = await google_signin.consume_details(session, handle, binding)
        live = await _live(session, await _find(session, raw))
        email = live.row.email
        if identity.email.strip().lower() != email:
            raise InputError(ErrorCode.VALIDATION_FAILED, "google")
        await set_gucs(session, login_email=email)
        user_id = await UserRepository(session).insert_google_invitee(
            email=email, sub=identity.sub, display_name=display_name or identity.display_name
        )
        if user_id is None:
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "invitations_account")
        await _accept(session, live, user_id)
    return Accepted(email=email, tenant_name=live.tenant_name)
