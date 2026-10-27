"""Identity repositories: users, memberships, refresh tokens and the audit writer (DATABASE.md
§5.1-§5.2, :306-312; CODE-PLAN S10 Q4, Q10).

These are the named exception to `TenantScopedRepository` (BACKEND-ARCHITECTURE.md §9): login,
refresh and switch read the caller's own memberships and tokens before, or across, a tenant. Each
statement is stamped with `identity_scope()` and still names its tenant wherever it has one; FORCE
RLS (0005) narrows every read to the caller's own rows through the GUCs the service sets first:
`app.login_email` (users_login), `app.user_id` (memberships_own, tenants_switcher, users_self),
`app.token_hash` (refresh_tokens_hash) and `app.tenant_id` (every write: Postgres checks a locking
read and an UPDATE against the UPDATE policy, so a write with no tenant GUC finds 0 rows).

Writes that must touch rows raise `RowCountError` when they do not (DATABASE.md:133).

DPP-1 (ADR-0021) adds the lockout calls (`begin_auth_attempt`, `note_auth_failure`,
`clear_auth_failures`: the 0011 SECURITY DEFINER functions), the password writes and history read,
the session proof (`amr`, `mfa_at`) on refresh rows, the step-up update of one family and the
revocation of every family of a user.
"""

from __future__ import annotations

import builtins
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import and_, delete, func, insert, literal, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.errors import RowCountError
from app.core.request_context import current_request_id
from app.db import staff_ops
from app.db.guard import TENANT_INFO_KEY
from app.db.models.ops import OAuthState, RefreshToken
from app.db.models.tenancy import (
    SignupRequest,
    Tenant,
    TenantMembership,
    User,
    UserPasswordHistory,
)
from app.db.session import identity_scope, set_gucs
from app.repositories.audit import AUDITED_ENTITIES

__all__ = [
    "AUDITED_ENTITIES",
    "AUTH_BUSY",
    "AuditRepository",
    "AuthBusy",
    "AuthState",
    "ConsumedState",
    "MembershipRepository",
    "MembershipRow",
    "RefreshRow",
    "RefreshTokenRepository",
    "SignupRepository",
    "SignupRow",
    "SlugTakenError",
    "UserRepository",
    "UserRow",
    "consume_oauth_state",
]

LIVE_TENANT_STATUSES: Final = ("trial", "active")
SlugTakenError = staff_ops.SlugTakenError


@dataclass(frozen=True, slots=True)
class UserRow:
    id: uuid.UUID
    email: str
    hashed_password: str | None = field(repr=False)
    is_active: bool
    display_name: str | None
    # DPP-1 (0011): read for the sign-in decision; the lock decision uses `AuthState` instead
    failed_auth_count: int = 0
    locked_until: datetime | None = None
    password_changed_at: datetime | None = None
    password_must_change: bool = False
    is_platform_admin: bool = False
    last_login_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuthState:
    """The row-locked lockout state `app_begin_auth_attempt` returns (DPP-1)."""

    failed_auth_count: int
    locked_until: datetime | None


class AuthBusy:
    """`app_begin_auth_attempt` waited past its 5 s lock_timeout (55P03): count nothing."""

    def __repr__(self) -> str:
        return "AUTH_BUSY"


AUTH_BUSY: Final = AuthBusy()
LOCK_NOT_AVAILABLE: Final = "55P03"


@dataclass(frozen=True, slots=True)
class MembershipRow:
    tenant_id: uuid.UUID
    tenant_name: str
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class RefreshRow:
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    family_id: uuid.UUID
    revoked_at: datetime | None
    expires_at: datetime
    # DPP-1 (0011): how the family was proven; None for a family minted with MFA off
    amr: builtins.list[str] | None = None
    mfa_at: datetime | None = None


# the history rows kept beside the current hash: current + 9 = the last 10 (DPP 1.4.1)
PASSWORD_HISTORY_KEPT: Final = 9


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", -1))


def _user_row(user: User) -> UserRow:
    return UserRow(
        id=user.id,
        email=user.email,
        hashed_password=user.hashed_password,
        is_active=user.is_active,
        display_name=user.display_name,
        failed_auth_count=user.failed_auth_count,
        locked_until=user.locked_until,
        password_changed_at=user.password_changed_at,
        password_must_change=user.password_must_change,
        is_platform_admin=user.is_platform_admin,
        last_login_at=user.last_login_at,
    )


class UserRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def by_email(self, email: str) -> UserRow | None:
        """Visible only under `app.login_email` = lower(email) (users_login)."""
        stmt = identity_scope(
            select(User)
            .options(undefer(User.hashed_password))
            .where(func.lower(User.email) == email.strip().lower())
        )
        user = (await self.db.execute(stmt)).scalar_one_or_none()
        return None if user is None else _user_row(user)

    async def self_row(self, user_id: uuid.UUID) -> UserRow | None:
        """The caller's own row with its password hash (users_self needs `app.user_id`)."""
        stmt = identity_scope(
            select(User).options(undefer(User.hashed_password)).where(User.id == user_id)
        )
        user = (await self.db.execute(stmt)).scalar_one_or_none()
        return None if user is None else _user_row(user)

    async def lock_login_email(self, email: str) -> bool:
        """Review E2E-2: a transaction-scoped advisory lock on the lower-cased email, taken on
        every public password path before the lookup and the verify. Parallel attempts for one
        email queue the same way whether or not it has an account (the users row lock alone
        queued only known emails: a timing oracle). False after a 5 s wait (55P03, rolled back
        to a savepoint, like `begin_auth_attempt`); the session's lock_timeout is put back."""
        previous = (
            await self.db.execute(text("SELECT current_setting('lock_timeout')"))
        ).scalar_one()
        try:
            async with self.db.begin_nested():
                await self.db.execute(text("SELECT set_config('lock_timeout', '5s', true)"))
                await self.db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended('login:' || :e, 0))"),
                    {"e": email.strip().lower()},
                )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE:
                return False
            raise
        finally:
            await self.db.execute(
                text("SELECT set_config('lock_timeout', :v, true)"), {"v": previous}
            )
        return True

    async def begin_auth_attempt(self, user_id: uuid.UUID) -> AuthState | AuthBusy | None:
        """DPP-1: take the user row FOR UPDATE through `app_begin_auth_attempt` and return its
        current counter and lock (None for no such user). The row lock serialises this user's
        sign-in attempts until the session's transaction ends. The call runs in a SAVEPOINT, so
        a 55P03 from the function's 5 s lock_timeout rolls back only the savepoint, leaves the
        outer transaction usable and returns `AUTH_BUSY`."""
        stmt = text("SELECT failed_auth_count, locked_until FROM public.app_begin_auth_attempt(:u)")
        try:
            async with self.db.begin_nested():
                row = (await self.db.execute(stmt, {"u": user_id})).one_or_none()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE:
                return AUTH_BUSY
            raise
        return None if row is None else AuthState(int(row[0]), row[1])

    async def note_auth_failure(self, user_id: uuid.UUID) -> bool:
        """Count one failed password or code; True when this failure locked the account."""
        stmt = text("SELECT public.app_note_auth_failure(:u)")
        return bool((await self.db.execute(stmt, {"u": user_id})).scalar_one())

    async def clear_auth_failures(self, user_id: uuid.UUID) -> None:
        """Reset the counter and lock after a full sign-in (only for `app.user_id`)."""
        await self.db.execute(text("SELECT public.app_clear_auth_failures(:u)"), {"u": user_id})

    async def set_password(
        self, user_id: uuid.UUID, password_hash: str, *, must_change: bool
    ) -> None:
        """A new password on the caller's own row (users_self), its age restarted."""
        stmt = identity_scope(
            update(User)
            .where(User.id == user_id)
            .values(
                hashed_password=password_hash,
                password_changed_at=func.now(),
                password_must_change=must_change,
            )
        ).execution_options(synchronize_session=False)
        result = await self.db.execute(stmt)
        if _rowcount(result) != 1:
            raise RowCountError(1, _rowcount(result))

    async def set_must_change(self, user_id: uuid.UUID) -> None:
        """`password_must_change = true` on the caller's own row (the password step under `all`
        with a password that breaks the current rules, before a code step answers)."""
        stmt = identity_scope(
            update(User).where(User.id == user_id).values(password_must_change=True)
        ).execution_options(synchronize_session=False)
        result = await self.db.execute(stmt)
        if _rowcount(result) != 1:
            raise RowCountError(1, _rowcount(result))

    async def touch_mfa_changed(self, user_id: uuid.UUID) -> None:
        """`mfa_last_changed_at = now()` on the caller's own row (a factor bound or replaced)."""
        stmt = identity_scope(
            update(User).where(User.id == user_id).values(mfa_last_changed_at=func.now())
        ).execution_options(synchronize_session=False)
        result = await self.db.execute(stmt)
        if _rowcount(result) != 1:
            raise RowCountError(1, _rowcount(result))

    async def password_hashes(self, user_id: uuid.UUID) -> builtins.list[str]:
        """The current hash, then the kept history newest first: at most 10 (DPP 1.4.1)."""
        current = await self.self_row(user_id)
        hashes = [current.hashed_password] if current and current.hashed_password else []
        stmt = (
            select(UserPasswordHistory.hashed_password)
            .where(UserPasswordHistory.user_id == user_id)
            .order_by(UserPasswordHistory.created_at.desc(), UserPasswordHistory.id.desc())
            .limit(PASSWORD_HISTORY_KEPT)
        )
        hashes += [str(h) for h in (await self.db.execute(stmt)).scalars().all()]
        return hashes

    async def is_platform_admin(self, user_id: uuid.UUID) -> bool:
        """The caller's own flag (users_self needs `app.user_id` = `user_id`); False for an
        inactive or unknown user."""
        stmt = identity_scope(
            select(User.is_platform_admin).where(User.id == user_id, User.is_active.is_(True))
        )
        return bool((await self.db.execute(stmt)).scalar_one_or_none())

    async def insert_signup_user(
        self, *, user_id: uuid.UUID, email: str, password_hash: str, display_name: str
    ) -> bool:
        """A new user from the public sign-up (users_signup needs `app.login_email` = the
        lower-cased email); False when a concurrent sign-up of the same email took
        `uq_users_email_lower` first (nothing written). RETURNING passes users_login (the same
        GUC). `is_platform_admin` keeps its default (the 0010 trigger refuses anything else)."""
        stmt = identity_scope(
            pg_insert(User)
            .values(
                id=user_id,
                email=email,
                hashed_password=password_hash,
                display_name=display_name,
            )
            .on_conflict_do_nothing()
            .returning(User.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def touch_login(self, user_id: uuid.UUID) -> None:
        """`last_login_at = now()` on the caller's own row (users_self needs `app.user_id`)."""
        stmt = identity_scope(
            update(User).where(User.id == user_id).values(last_login_at=func.now())
        ).execution_options(synchronize_session=False)
        result = await self.db.execute(stmt)
        if _rowcount(result) != 1:
            raise RowCountError(1, _rowcount(result))


def _membership_select() -> Any:
    return (
        select(
            TenantMembership.tenant_id,
            Tenant.name,
            TenantMembership.user_id,
            User.email,
            User.display_name,
            TenantMembership.role,
            TenantMembership.last_used_at,
        )
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .join(User, User.id == TenantMembership.user_id)
        .where(
            TenantMembership.is_active.is_(True),
            User.is_active.is_(True),
            Tenant.status.in_(LIVE_TENANT_STATUSES),
        )
    )


def _membership_row(row: Any) -> MembershipRow:
    return MembershipRow(
        tenant_id=row[0],
        tenant_name=row[1],
        user_id=row[2],
        email=row[3],
        display_name=row[4],
        role=row[5],
        last_used_at=row[6],
    )


class MembershipRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_active_for_user(self, user_id: uuid.UUID) -> builtins.list[MembershipRow]:
        """The caller's active memberships in live tenants, most recently used first. Needs
        `app.user_id` = `user_id` (memberships_own, tenants_switcher, users_self)."""
        stmt = identity_scope(
            _membership_select()
            .where(TenantMembership.user_id == user_id)
            .order_by(
                TenantMembership.last_used_at.desc().nulls_last(),
                TenantMembership.created_at,
                TenantMembership.tenant_id,
            )
        )
        return [_membership_row(row) for row in (await self.db.execute(stmt)).all()]

    async def probe(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> MembershipRow | None:
        """The one-lookup membership probe (DATABASE.md:312): an active membership of an active
        user in a `trial`/`active` tenant, or None. The role is the row's, never the token's."""
        stmt = identity_scope(
            _membership_select().where(
                TenantMembership.tenant_id == tenant_id, TenantMembership.user_id == user_id
            )
        )
        row = (await self.db.execute(stmt)).one_or_none()
        return None if row is None else _membership_row(row)

    async def touch_last_used(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """`last_used_at = now()`; needs `app.tenant_id` = `tenant_id` (the UPDATE policy)."""
        stmt = identity_scope(
            update(TenantMembership)
            .where(TenantMembership.tenant_id == tenant_id, TenantMembership.user_id == user_id)
            .values(last_used_at=func.now())
        ).execution_options(synchronize_session=False)
        result = await self.db.execute(stmt)
        if _rowcount(result) != 1:
            raise RowCountError(1, _rowcount(result))


class RefreshTokenRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def insert(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        family_id: uuid.UUID,
        token_hash: bytes,
        expires_at: datetime,
        ip: str | None = None,
        user_agent: str | None = None,
        amr: builtins.list[str] | None = None,
        mfa_at: datetime | None = None,
    ) -> uuid.UUID:
        """A new refresh row (id chosen here, so no RETURNING has to pass a SELECT policy)."""
        row_id = uuid.uuid4()
        stmt = identity_scope(
            insert(RefreshToken).values(
                id=row_id,
                tenant_id=tenant_id,
                user_id=user_id,
                family_id=family_id,
                token_hash=token_hash,
                expires_at=expires_at,
                ip=ip,
                user_agent=user_agent,
                amr=amr,
                mfa_at=mfa_at,
            )
        )
        await self.db.execute(stmt)
        return row_id

    async def by_hash_for_update(self, token_hash: bytes) -> RefreshRow | None:
        """The row for `token_hash`, locked. Callers set `app.tenant_id` (and `app.user_id`) from
        the verified token first (Q10): with no tenant GUC the locking read finds nothing."""
        stmt = identity_scope(
            select(
                RefreshToken.id,
                RefreshToken.tenant_id,
                RefreshToken.user_id,
                RefreshToken.family_id,
                RefreshToken.revoked_at,
                RefreshToken.expires_at,
                RefreshToken.amr,
                RefreshToken.mfa_at,
            )
            .where(RefreshToken.token_hash == token_hash)
            .with_for_update()
        )
        row = (await self.db.execute(stmt)).one_or_none()
        return None if row is None else RefreshRow(*row)

    async def rotate(self, tenant_id: uuid.UUID, old_id: uuid.UUID, new_id: uuid.UUID) -> None:
        """Retire `old_id` as `rotated`, pointing at its successor; exactly one live row."""
        stmt = identity_scope(
            update(RefreshToken)
            .where(
                RefreshToken.tenant_id == tenant_id,
                RefreshToken.id == old_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=func.now(), revoked_reason="rotated", replaced_by=new_id)
        ).execution_options(synchronize_session=False)
        result = await self.db.execute(stmt)
        if _rowcount(result) != 1:
            raise RowCountError(1, _rowcount(result))

    async def revoke_family(
        self, tenant_id: uuid.UUID, family_id: uuid.UUID, reason: str, *, expect_live: bool
    ) -> int:
        """Revoke every live row of the family; the count. `expect_live` makes 0 rows an error
        (a logout of a live token must revoke something)."""
        return await self._revoke(
            and_(RefreshToken.tenant_id == tenant_id, RefreshToken.family_id == family_id),
            reason,
            expect_live=expect_live,
        )

    async def revoke_membership(self, tenant_id: uuid.UUID, user_id: uuid.UUID, reason: str) -> int:
        """Revoke every live token of one membership (deactivation); 0 is fine."""
        return await self._revoke(
            and_(RefreshToken.tenant_id == tenant_id, RefreshToken.user_id == user_id),
            reason,
            expect_live=False,
        )

    async def step_up_family(
        self,
        tenant_id: uuid.UUID,
        family_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        amr: builtins.list[str],
        mfa_at: datetime,
    ) -> int:
        """DPP-1: record a fresh code on the live rows of one family of `user_id` (the step-up
        and a bearer enrolment); the count. AUTH-3's two statements: a locking read waits for a
        concurrent refresh holding the predecessor, then the UPDATE (a fresh snapshot) sees the
        committed successor, so a rotation in flight never makes the step-up answer 401."""
        where = and_(
            RefreshToken.tenant_id == tenant_id,
            RefreshToken.family_id == family_id,
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        await self.db.execute(
            identity_scope(select(RefreshToken.id).where(where).with_for_update())
        )
        stmt = identity_scope(
            update(RefreshToken).where(where).values(amr=amr, mfa_at=mfa_at)
        ).execution_options(synchronize_session=False)
        return _rowcount(await self.db.execute(stmt))

    async def revoke_user_everywhere(
        self, user_id: uuid.UUID, *, keep_family: uuid.UUID | None, reason: str
    ) -> int:
        """DPP-1: revoke every live refresh family of `user_id` in every tenant it belongs to,
        except `keep_family`; the count. Needs `app.user_id` = `user_id` (memberships_own). Each
        tenant is written under its own `app.tenant_id` with AUTH-3's locking read then UPDATE;
        the caller's tenant GUC is put back afterwards."""
        tenants = (
            (
                await self.db.execute(
                    identity_scope(
                        select(TenantMembership.tenant_id).where(
                            TenantMembership.user_id == user_id
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        previous = self.db.info.get(TENANT_INFO_KEY)
        total = 0
        try:
            for tenant_id in tenants:
                await set_gucs(self.db, tenant_id=tenant_id)
                where = and_(
                    RefreshToken.tenant_id == tenant_id,
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked_at.is_(None),
                )
                if keep_family is not None:
                    where = and_(where, RefreshToken.family_id != keep_family)
                await self.db.execute(
                    identity_scope(select(RefreshToken.id).where(where).with_for_update())
                )
                stmt = identity_scope(
                    update(RefreshToken)
                    .where(where)
                    .values(revoked_at=func.now(), revoked_reason=reason)
                ).execution_options(synchronize_session=False)
                total += _rowcount(await self.db.execute(stmt))
        finally:
            await _restore_tenant(self.db, previous)
        return total

    async def _revoke(self, where: Any, reason: str, *, expect_live: bool) -> int:
        stmt = identity_scope(
            update(RefreshToken)
            .where(where, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=func.now(), revoked_reason=reason)
        ).execution_options(synchronize_session=False)
        count = _rowcount(await self.db.execute(stmt))
        if expect_live and count < 1:
            raise RowCountError(1, count)
        return count


async def _restore_tenant(session: AsyncSession, previous: Any) -> None:
    """Put the caller's `app.tenant_id` back (unset when it had none)."""
    if previous is not None:
        await set_gucs(session, tenant_id=previous)
        return
    session.info.pop(TENANT_INFO_KEY, None)
    await session.execute(select(func.set_config("app.tenant_id", "", True)))


@dataclass(frozen=True, slots=True)
class SignupRow:
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    brand_name: str
    owner_name: str
    phone: str | None
    website: str | None
    status: str
    reason: str | None
    created_tenant_id: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


def _signup_select() -> Any:
    return select(
        SignupRequest.id,
        SignupRequest.user_id,
        User.email,
        SignupRequest.brand_name,
        SignupRequest.owner_name,
        SignupRequest.phone,
        SignupRequest.website,
        SignupRequest.status,
        SignupRequest.reason,
        SignupRequest.created_tenant_id,
        SignupRequest.decided_at,
        SignupRequest.created_at,
    ).join(User, User.id == SignupRequest.user_id)


class SignupRepository:
    """`signup_requests` (0010; ADR-0019). RLS narrows every statement: the applicant sees and
    inserts only their own rows (`app.user_id`); a platform admin (`app_is_platform_admin()`)
    reads and decides any row."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def insert_pending(
        self,
        *,
        user_id: uuid.UUID,
        brand_name: str,
        owner_name: str,
        phone: str | None,
        website: str | None,
    ) -> uuid.UUID | None:
        """A new pending request, or None when the user already has one (the partial unique
        index `uq_signup_requests_pending` takes the race)."""
        row_id = uuid.uuid4()
        stmt = identity_scope(
            pg_insert(SignupRequest)
            .values(
                id=row_id,
                user_id=user_id,
                brand_name=brand_name,
                owner_name=owner_name,
                phone=phone,
                website=website,
            )
            .on_conflict_do_nothing(
                index_elements=[SignupRequest.user_id],
                # a literal predicate: the partial index is inferred at plan time, never from a
                # bound parameter
                index_where=text("status = 'pending'"),
            )
            .returning(SignupRequest.id)
        )
        found = (await self.db.execute(stmt)).scalar_one_or_none()
        return None if found is None else row_id

    async def latest_for(
        self, user_id: uuid.UUID, *, pending_only: bool = False
    ) -> SignupRow | None:
        """The user's newest request (or newest pending one)."""
        stmt = _signup_select().where(SignupRequest.user_id == user_id)
        if pending_only:
            stmt = stmt.where(SignupRequest.status == "pending")
        stmt = identity_scope(
            stmt.order_by(SignupRequest.created_at.desc(), SignupRequest.id.desc()).limit(1)
        )
        row = (await self.db.execute(stmt)).one_or_none()
        return None if row is None else SignupRow(*row)

    async def page(
        self,
        *,
        status: str | None,
        limit: int,
        after: tuple[datetime, uuid.UUID] | None,
    ) -> builtins.list[SignupRow]:
        """Up to `limit + 1` rows, oldest first on `(created_at, id)` (the caller trims)."""
        stmt = _signup_select()
        if status is not None:
            stmt = stmt.where(SignupRequest.status == status)
        if after is not None:
            at, row_id = after
            bound = tuple_(
                literal(at, SignupRequest.created_at.type), literal(row_id, SignupRequest.id.type)
            )
            stmt = stmt.where(tuple_(SignupRequest.created_at, SignupRequest.id) > bound)
        stmt = identity_scope(
            stmt.order_by(SignupRequest.created_at, SignupRequest.id).limit(limit + 1)
        )
        return [SignupRow(*row) for row in (await self.db.execute(stmt)).all()]

    async def lock(self, request_id: uuid.UUID) -> SignupRow | None:
        stmt = identity_scope(
            _signup_select().where(SignupRequest.id == request_id).with_for_update(of=SignupRequest)
        )
        row = (await self.db.execute(stmt)).one_or_none()
        return None if row is None else SignupRow(*row)

    async def decide(
        self,
        request_id: uuid.UUID,
        *,
        status: str,
        reason: str | None,
        decided_by: uuid.UUID,
        created_tenant_id: uuid.UUID | None,
    ) -> None:
        """Move a pending request to `approved` or `rejected`; exactly one row."""
        stmt = identity_scope(
            update(SignupRequest)
            .where(SignupRequest.id == request_id, SignupRequest.status == "pending")
            .values(
                status=status,
                reason=reason,
                decided_by_user_id=decided_by,
                decided_at=func.now(),
                created_tenant_id=created_tenant_id,
            )
        ).execution_options(synchronize_session=False)
        result = await self.db.execute(stmt)
        if _rowcount(result) != 1:
            raise RowCountError(1, _rowcount(result))

    async def create_tenant(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        slug: str,
        trial_days: int,
        owner_id: uuid.UUID,
        staff_email: str,
    ) -> None:
        """The tenant and its owner membership through the staff CLI's own statements
        (`staff_ops.insert_tenant` and `add_owner`), on this session's connection as app_rw.
        The caller has set `app.tenant_id` = `tenant_id` (the t_tenant policies). A used slug is
        `SlugTakenError` with nothing written, so the caller may try another."""
        conn = await self.db.connection()
        request_id = current_request_id() or "-"
        await staff_ops.insert_tenant(
            conn,
            tenant_id=tenant_id,
            name=name,
            slug=slug,
            staff_email=staff_email,
            trial_days=trial_days,
            request_id=request_id,
        )
        await staff_ops.add_owner(
            conn,
            tenant_id=tenant_id,
            user_id=owner_id,
            staff_email=staff_email,
            request_id=request_id,
        )


@dataclass(frozen=True, slots=True)
class ConsumedState:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    marketplace: str
    code_verifier: str | None = field(repr=False)


async def consume_oauth_state(
    session: AsyncSession, *, state_hash: bytes, marketplace: str
) -> ConsumedState | None:
    """The OAuth callback's one-time consume (DATABASE.md:1017-1018; S15): before any tenant is
    known, `app.token_hash` names the state row, and one `DELETE ... RETURNING` takes it if it is
    a live state row (not a pending row) for this marketplace. The 0006 hash branches
    (`oauth_states_hash_select` / `_delete`) also require `token_enc IS NULL`; the filter is kept
    here so the intent reads in one place. A forged, expired, replayed or mismatched state is None
    (and a mismatched one stays unconsumed)."""
    await set_gucs(session, token_hash=state_hash)
    stmt = identity_scope(
        delete(OAuthState)
        .where(
            OAuthState.state_hash == state_hash,
            OAuthState.marketplace == marketplace,
            OAuthState.token_enc.is_(None),
            OAuthState.expires_at > func.now(),
        )
        .returning(
            OAuthState.tenant_id,
            OAuthState.user_id,
            OAuthState.marketplace,
            OAuthState.code_verifier,
        )
    ).execution_options(synchronize_session=False)
    row = (await session.execute(stmt)).one_or_none()
    return None if row is None else ConsumedState(row[0], row[1], row[2], row[3])


_AUDIT_SQL: Final = text(
    "SELECT public.audit(:tenant_id, :actor_type, :actor_user_id, :action, :entity_type, "
    ":entity_id, CAST(:before AS jsonb), CAST(:after AS jsonb), :reason, :request_id, "
    "NULL, CAST(:ip AS inet), :actor_email)"
)


class AuditRepository:
    """One `audit_log` row through `audit()` (DATABASE.md:302): the database keeps only the
    entity's allow-listed keys, and INSERTs without RETURNING."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def write(
        self,
        *,
        tenant_id: uuid.UUID | None,
        actor_type: str,
        actor_user_id: uuid.UUID | None,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None,
        before: str | None = None,
        after: str | None = None,
        reason: str | None = None,
        ip: str | None = None,
        actor_email: str | None = None,
    ) -> None:
        """`actor_email` names a staff actor (`actor_type='staff'`, DATABASE.md:312)."""
        if entity_type not in AUDITED_ENTITIES:
            raise ValueError(f"{entity_type} is not in AUDITED_ENTITIES")
        await self.db.execute(
            _AUDIT_SQL,
            {
                "tenant_id": tenant_id,
                "actor_type": actor_type,
                "actor_user_id": actor_user_id,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "before": before,
                "after": after,
                "reason": reason,
                "request_id": current_request_id(),
                "ip": ip,
                "actor_email": actor_email,
            },
        )
