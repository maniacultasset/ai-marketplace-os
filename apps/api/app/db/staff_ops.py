"""Staff tenant onboarding (S16 T19-T20a; PLAN D6; CP9; DATABASE.md:312): the SQL behind
`python -m app.cli tenant create|add-member` and `user set-password|make-admin|unlock|reset-mfa`
(repo rule R4: the CLI holds no SQL; DPP-1 adds the last two, ADR-0021). `insert_tenant` and `add_owner` also serve the platform admin's sign-up
approval (ADR-0019), which runs them on the app's own connection as app_rw under the new
tenant's GUC, so a CLI tenant and an approved sign-up are created by the same statements.

Each operation is ONE transaction as `migrator` on the admin URL (`ALEMBIC_DATABASE_URL`), with
`app.tenant_id` set to the tenant first, so the `t_migrator` policies and the append-only and
last-owner guards apply exactly as for any other write; every audit row is `actor_type='staff'`
with the staff email and the minted request id. Nothing here logs or returns a password or hash.

DPP-1: every password staff set (a new user, `set-password`, `reset-mfa`) must meet the DPP 1.4.1
rules (`PolicyError`, exit 2) and is must-change: the user picks their own at the next sign-in
under `MFA_ENFORCEMENT=all`. `set-password` and `reset-mfa` also refuse any of the last 10.

ADR-0023: `set-password`, `reset-mfa` and `make-admin` forget every trusted browser of the user
(the next sign-in asks for the code; an admin's new browsers last 7 days). `unlock` resets
nothing and keeps them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core import password_policy
from app.core.passwords import hash_password, verify_password

RUNNING: Final = ("trial", "active")


class SlugTakenError(Exception):
    """`tenants.slug` is already used (exit 4). The message names no value."""


class NotEligibleError(Exception):
    """An unknown tenant or user, a tenant not `trial|active`, or an existing membership
    (exit 4). The message is a fixed phrase."""


@dataclass(frozen=True, slots=True)
class NewUser:
    """A user to create when the email has none; `password_hash` is already argon2id."""

    email: str
    password_hash: str | None


async def _as_migrator(conn: AsyncConnection, tenant_id: uuid.UUID) -> None:
    current = (await conn.execute(text("SELECT current_user"))).scalar_one()
    if current != "migrator":
        await conn.execute(text("SET LOCAL ROLE migrator"))
    await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})


async def _audit(
    conn: AsyncConnection,
    *,
    tenant_id: uuid.UUID | None,
    staff_email: str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    after: str | None,
    request_id: str,
) -> None:
    await conn.execute(
        text(
            "SELECT public.audit(:t, 'staff', NULL, :action, :entity_type, :entity_id, NULL, "
            "CAST(:after AS jsonb), NULL, :request_id, NULL, NULL, :email)"
        ),
        {
            "t": tenant_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "after": after,
            "request_id": request_id,
            "email": staff_email,
        },
    )


async def user_exists(engine: AsyncEngine, email: str) -> bool:
    """Whether a user with this email exists (lower-cased match), read before a password prompt."""
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT 1 FROM users WHERE lower(email) = lower(:e)"), {"e": email}
        )
        return found.first() is not None


async def _unvetted_applicant(conn: AsyncConnection, user_id: uuid.UUID) -> bool:
    """A user who came in through the public sign-up (ADR-0019) and holds no membership yet. Its
    password was chosen by whoever filled the form, and nothing proved they own the email, so
    `tenant create` / `add-member` must not hand that login a tenant: the platform admin vets the
    applicant in the queue instead."""
    found = await conn.execute(
        text(
            "SELECT 1 WHERE EXISTS (SELECT 1 FROM signup_requests WHERE user_id = :u) "
            "AND NOT EXISTS (SELECT 1 FROM tenant_memberships WHERE user_id = :u)"
        ),
        {"u": user_id},
    )
    return found.first() is not None


async def _user_id(conn: AsyncConnection, user: NewUser) -> uuid.UUID:
    found = (
        await conn.execute(
            text("SELECT id, is_active, erased_at FROM users WHERE lower(email) = lower(:e)"),
            {"e": user.email},
        )
    ).first()
    if found is not None:
        if not found.is_active or found.erased_at is not None:
            raise NotEligibleError("the user is inactive or erased")
        if await _unvetted_applicant(conn, found.id):
            raise NotEligibleError(
                "a self-serve applicant with no tenant: decide the sign-up in the admin queue"
            )
        return found.id  # type: ignore[no-any-return]
    if user.password_hash is None:
        raise NotEligibleError("a new user needs a password")
    new_id = uuid.uuid4()
    # DPP-1 (CODE-PLAN Q1): a password staff hand over is must-change, like `set-password`
    await conn.execute(
        text(
            "INSERT INTO users (id, email, hashed_password, password_must_change) "
            "VALUES (:id, :e, :h, true)"
        ),
        {"id": new_id, "e": user.email, "h": user.password_hash},
    )
    return new_id


async def _member(
    conn: AsyncConnection,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    staff_email: str,
    request_id: str,
) -> None:
    exists = (
        await conn.execute(
            text("SELECT 1 FROM tenant_memberships WHERE tenant_id = :t AND user_id = :u"),
            {"t": tenant_id, "u": user_id},
        )
    ).first()
    if exists is not None:
        raise NotEligibleError("the membership exists")
    await conn.execute(
        text("INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
        {"t": tenant_id, "u": user_id, "r": role},
    )
    await _audit(
        conn,
        tenant_id=tenant_id,
        staff_email=staff_email,
        action="membership.created",
        entity_type="membership",
        entity_id=user_id,
        after=_json({"role": role, "is_active": True}),
        request_id=request_id,
    )


async def insert_tenant(
    conn: AsyncConnection,
    *,
    tenant_id: uuid.UUID,
    name: str,
    slug: str,
    staff_email: str,
    trial_days: int | None,
    request_id: str,
) -> None:
    """The tenant row and its `tenant.created` staff audit row, on a connection whose
    `app.tenant_id` is already `tenant_id`. `SlugTakenError` (nothing written) when the slug is
    used. Shared by `tenant create` and the platform admin's sign-up approval (ADR-0019), so both
    create a tenant the same way."""
    status = "active" if trial_days is None else "trial"
    trial_ends = None if trial_days is None else datetime.now(UTC) + timedelta(days=trial_days)
    inserted = (
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, slug, status, trial_ends_at) "
                "VALUES (:id, :name, :slug, :status, :trial) "
                "ON CONFLICT (slug) DO NOTHING RETURNING id"
            ),
            {"id": tenant_id, "name": name, "slug": slug, "status": status, "trial": trial_ends},
        )
    ).first()
    if inserted is None:
        raise SlugTakenError("slug taken")
    await _audit(
        conn,
        tenant_id=tenant_id,
        staff_email=staff_email,
        action="tenant.created",
        entity_type="tenant",
        entity_id=tenant_id,
        after=_json({"name": name}),
        request_id=request_id,
    )


async def add_owner(
    conn: AsyncConnection,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    staff_email: str,
    request_id: str,
) -> None:
    """The first owner membership and its `membership.created` staff audit row."""
    await _member(
        conn,
        tenant_id=tenant_id,
        user_id=user_id,
        role="owner",
        staff_email=staff_email,
        request_id=request_id,
    )


async def create_tenant_with_owner(
    engine: AsyncEngine,
    *,
    name: str,
    slug: str,
    owner: NewUser,
    staff_email: str,
    trial_days: int | None,
    request_id: str,
) -> uuid.UUID:
    tenant_id = uuid.uuid4()  # fixed before the transaction opens (S9 D16)
    async with engine.begin() as conn:
        await _as_migrator(conn, tenant_id)
        await insert_tenant(
            conn,
            tenant_id=tenant_id,
            name=name,
            slug=slug,
            staff_email=staff_email,
            trial_days=trial_days,
            request_id=request_id,
        )
        user_id = await _user_id(conn, owner)
        await add_owner(
            conn,
            tenant_id=tenant_id,
            user_id=user_id,
            staff_email=staff_email,
            request_id=request_id,
        )
    return tenant_id


async def add_member(
    engine: AsyncEngine,
    *,
    slug: str,
    user: NewUser,
    role: str,
    staff_email: str,
    request_id: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT id, status FROM tenants WHERE slug = :s"), {"s": slug})
        ).first()
    if row is None or row.status not in RUNNING:
        raise NotEligibleError("unknown or stopped tenant")
    tenant_id: uuid.UUID = row.id
    async with engine.begin() as conn:
        await _as_migrator(conn, tenant_id)
        status = (
            await conn.execute(
                text("SELECT status FROM tenants WHERE id = :t FOR UPDATE"), {"t": tenant_id}
            )
        ).scalar_one_or_none()
        if status not in RUNNING:
            raise NotEligibleError("unknown or stopped tenant")
        user_id = await _user_id(conn, user)
        await _member(
            conn,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            staff_email=staff_email,
            request_id=request_id,
        )
    return tenant_id, user_id


async def _locked_user(conn: AsyncConnection, email: str) -> Any:
    current = (await conn.execute(text("SELECT current_user"))).scalar_one()
    if current != "migrator":
        await conn.execute(text("SET LOCAL ROLE migrator"))
    found = (
        await conn.execute(
            text(
                "SELECT id, email, display_name, hashed_password FROM users "
                "WHERE lower(email) = lower(:e) FOR UPDATE"
            ),
            {"e": email},
        )
    ).first()
    if found is None:
        raise NotEligibleError("unknown user")
    return found


async def _store_staff_password(conn: AsyncConnection, user: Any, password: SecretStr) -> None:
    """DPP-1: the rules, none of the last 10, then the new must-change hash; the old one joins
    the history (9 kept), and every sign-in challenge and (ADR-0023) every trusted browser of the
    user is deleted."""
    plain = password.get_secret_value()
    breaches = password_policy.check(plain, email=user.email, display_name=user.display_name)
    if breaches:
        raise password_policy.PolicyError(breaches)
    history = (
        await conn.execute(
            text(
                "SELECT hashed_password FROM user_password_history WHERE user_id = :u "
                "ORDER BY created_at DESC, id DESC LIMIT 9"
            ),
            {"u": user.id},
        )
    ).scalars()
    previous = [user.hashed_password, *history] if user.hashed_password else list(history)
    if any(verify_password(plain, h) for h in previous if h):
        raise password_policy.reused()
    if user.hashed_password:
        await conn.execute(
            text("INSERT INTO user_password_history (user_id, hashed_password) VALUES (:u, :h)"),
            {"u": user.id, "h": user.hashed_password},
        )
        await conn.execute(
            text(
                "DELETE FROM user_password_history WHERE user_id = :u AND id NOT IN ("
                "SELECT id FROM user_password_history WHERE user_id = :u "
                "ORDER BY created_at DESC, id DESC LIMIT 9)"
            ),
            {"u": user.id},
        )
    await conn.execute(
        text(
            "UPDATE users SET hashed_password = :h, password_changed_at = now(), "
            "password_must_change = true WHERE id = :u"
        ),
        {"h": hash_password(plain), "u": user.id},
    )
    # Review E2E-1: a sign-in begun under the old password (an enroll or password_change
    # challenge) must not outlive it, or a leaked password survives the staff reset.
    await conn.execute(text("DELETE FROM public.mfa_challenges WHERE user_id = :u"), {"u": user.id})
    await _forget_browsers(conn, user.id)


async def _forget_browsers(conn: AsyncConnection, user_id: uuid.UUID) -> int:
    """ADR-0023: every trusted browser of the user deleted (as migrator); the count."""
    result: Any = await conn.execute(
        text("DELETE FROM public.trusted_devices WHERE user_id = :u"), {"u": user_id}
    )
    return int(result.rowcount or 0)


async def _revoke_everywhere(
    conn: AsyncConnection, user_id: uuid.UUID, *, staff_email: str, request_id: str, action: str
) -> int:
    """Every live refresh token of the user revoked (`password_changed`) in each of their
    tenants, with one `action` staff audit row per tenant."""
    tenants = (
        await conn.execute(
            text("SELECT tenant_id FROM tenant_memberships WHERE user_id = :u ORDER BY 1"),
            {"u": user_id},
        )
    ).scalars()
    revoked = 0
    for tenant_id in list(tenants):
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
        )
        # Two statements, not one (audit AUTH-3): under READ COMMITTED the UPDATE's snapshot
        # cannot see the successor an in-flight /auth/refresh has inserted but not yet
        # committed. The locking read waits on the predecessor that refresh holds FOR
        # UPDATE until that transaction commits; the UPDATE then takes a fresh snapshot in
        # which the successor is visible. A refresh that starts later blocks on this
        # transaction's locks and finds its row revoked (reuse detection).
        await conn.execute(
            text(
                "SELECT id FROM refresh_tokens WHERE tenant_id = :t AND user_id = :u "
                "AND revoked_at IS NULL FOR UPDATE"
            ),
            {"t": tenant_id, "u": user_id},
        )
        result: Any = await conn.execute(
            text(
                "UPDATE refresh_tokens SET revoked_at = now(), revoked_reason = 'password_changed' "
                "WHERE tenant_id = :t AND user_id = :u AND revoked_at IS NULL"
            ),
            {"t": tenant_id, "u": user_id},
        )
        revoked += int(result.rowcount or 0)
        await _audit(
            conn,
            tenant_id=tenant_id,
            staff_email=staff_email,
            action=action,
            entity_type="user",
            entity_id=user_id,
            after=None,
            request_id=request_id,
        )
    return revoked


async def set_password(
    engine: AsyncEngine, *, email: str, password: SecretStr, staff_email: str, request_id: str
) -> tuple[uuid.UUID, int]:
    """A new must-change password that meets the DPP rules and none of the last 10, every live
    refresh token of the user revoked (`password_changed`) in each of their tenants, and one
    `user.password_set` audit row per tenant; one transaction. `PolicyError` writes nothing."""
    async with engine.begin() as conn:
        user = await _locked_user(conn, email)
        await _store_staff_password(conn, user, password)
        revoked = await _revoke_everywhere(
            conn,
            user.id,
            staff_email=staff_email,
            request_id=request_id,
            action="user.password_set",
        )
    return user.id, revoked


async def unlock_user(
    engine: AsyncEngine, *, email: str, staff_email: str, request_id: str
) -> uuid.UUID:
    """DPP-1: clear the lockout counter and lock (after the runbook's identity check), with one
    platform `auth.unlocked` staff audit row; one transaction as migrator."""
    async with engine.begin() as conn:
        user = await _locked_user(conn, email)
        await conn.execute(
            text("UPDATE users SET failed_auth_count = 0, locked_until = NULL WHERE id = :u"),
            {"u": user.id},
        )
        await _audit(
            conn,
            tenant_id=None,
            staff_email=staff_email,
            action="auth.unlocked",
            entity_type="user",
            entity_id=user.id,
            after=None,
            request_id=request_id,
        )
    return user.id  # type: ignore[no-any-return]


async def reset_mfa(
    engine: AsyncEngine, *, email: str, password: SecretStr, staff_email: str, request_id: str
) -> tuple[uuid.UUID, int]:
    """DPP-1: a lost authenticator, after the runbook's identity check. In one transaction: the
    factor, recovery codes and challenges deleted, a new must-change password set (DPP rules,
    none of the last 10), the lock cleared, every session revoked, and one platform
    `auth.mfa_reset` staff audit row. The user enrols again at the next sign-in."""
    async with engine.begin() as conn:
        user = await _locked_user(conn, email)
        await _store_staff_password(conn, user, password)
        for table in ("user_mfa_factors", "user_recovery_codes"):  # challenges: above
            await conn.execute(
                text(f"DELETE FROM public.{table} WHERE user_id = :u"),  # noqa: S608 - constants
                {"u": user.id},
            )
        await conn.execute(
            text(
                "UPDATE users SET mfa_last_changed_at = now(), failed_auth_count = 0, "
                "locked_until = NULL WHERE id = :u"
            ),
            {"u": user.id},
        )
        revoked = await _revoke_everywhere(
            conn,
            user.id,
            staff_email=staff_email,
            request_id=request_id,
            action="user.password_set",
        )
        await _audit(
            conn,
            tenant_id=None,
            staff_email=staff_email,
            action="auth.mfa_reset",
            entity_type="user",
            entity_id=user.id,
            after=None,
            request_id=request_id,
        )
    return user.id, revoked


async def make_platform_admin(
    engine: AsyncEngine, *, email: str, staff_email: str, request_id: str
) -> uuid.UUID:
    """`users.is_platform_admin = true` for an active user (ADR-0019), as migrator, with one
    platform `user.platform_admin_granted` staff audit row, and every trusted browser of the user
    forgotten (ADR-0023: an admin trusts a browser for 7 days, never the 30 of a brand user).
    Idempotent: an admin stays one and gets no second audit row. `NotEligibleError` for an
    unknown, inactive or erased user."""
    async with engine.begin() as conn:
        current = (await conn.execute(text("SELECT current_user"))).scalar_one()
        if current != "migrator":
            await conn.execute(text("SET LOCAL ROLE migrator"))
        found = (
            await conn.execute(
                text(
                    "SELECT id, is_active, erased_at, is_platform_admin FROM users "
                    "WHERE lower(email) = lower(:e) FOR UPDATE"
                ),
                {"e": email},
            )
        ).first()
        if found is None or not found.is_active or found.erased_at is not None:
            raise NotEligibleError("unknown, inactive or erased user")
        user_id: uuid.UUID = found.id
        if found.is_platform_admin:
            return user_id
        await conn.execute(
            text("UPDATE users SET is_platform_admin = true WHERE id = :u"), {"u": user_id}
        )
        await _forget_browsers(conn, user_id)
        await _audit(
            conn,
            tenant_id=None,
            staff_email=staff_email,
            action="user.platform_admin_granted",
            entity_type="user",
            entity_id=user_id,
            after=None,
            request_id=request_id,
        )
    return user_id


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, sort_keys=True)
