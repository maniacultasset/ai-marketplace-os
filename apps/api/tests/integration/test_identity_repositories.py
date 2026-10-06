"""S10 C4 (AC1, AC3; CODE-PLAN Q4, Q10): the identity repositories, run as app_rw under FORCE RLS.

The world is built on the admin connection; every read and write below goes through the app
engine (`app_rw`), so a missing GUC shows up as a missing row, never as a silent bypass."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from sqlalchemy import text

from .auth_support import Conn, add_member, add_tenant, add_user, build_world
from .conftest import LOCAL_HOSTS, libpq_url

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _hash(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def _refresh(
    conn: Conn,
    tenant: uuid.UUID,
    user: uuid.UUID,
    family: uuid.UUID,
    label: str,
    *,
    revoked: bool = False,
) -> uuid.UUID:
    row = conn.execute(
        "INSERT INTO refresh_tokens (tenant_id, user_id, family_id, token_hash, expires_at, "
        "revoked_at, revoked_reason) VALUES (%s, %s, %s, %s, now() + interval '1 day', %s, %s) "
        "RETURNING id",
        [
            tenant,
            user,
            family,
            _hash(label),
            datetime.now(UTC) if revoked else None,
            "rotated" if revoked else None,
        ],
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


async def _probe(tenant: uuid.UUID, user: uuid.UUID) -> Any:
    from app.repositories.identity import MembershipRepository
    from app.repositories.transactions import unit_of_work

    async with unit_of_work(tenant, user_id=user) as session:
        return await MembershipRepository(session).probe(tenant, user)


async def test_probe_ok(auth_db: Conn) -> None:
    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    row = await _probe(acme, owner)
    assert row is not None
    assert (row.tenant_id, row.user_id, row.role, row.tenant_name) == (acme, owner, "owner", "Acme")
    assert row.email == "owner@acme.test"
    # another tenant's member is no member of acme
    assert await _probe(acme, world.user("owner@bharat.test")) is None


async def test_probe_inactive_membership(auth_db: Conn) -> None:
    world = build_world(auth_db)
    acme, reviewer = world.tenant("acme"), world.user("reviewer@acme.test")
    auth_db.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
        "WHERE tenant_id = %s AND user_id = %s",
        [acme, reviewer],
    )
    assert await _probe(acme, reviewer) is None


async def test_probe_inactive_user(auth_db: Conn) -> None:
    world = build_world(auth_db)
    reviewer = world.user("reviewer@acme.test")
    auth_db.execute("UPDATE users SET is_active = false WHERE id = %s", [reviewer])
    assert await _probe(world.tenant("acme"), reviewer) is None


async def _dead_tenant_probe(auth_db: Conn, status: str) -> None:
    tenant = add_tenant(auth_db, f"dead-{status}", status=status)
    user = add_user(auth_db, f"owner@{status}.test")
    add_member(auth_db, tenant, user, "owner")
    assert await _probe(tenant, user) is None


async def test_probe_suspended_tenant(auth_db: Conn) -> None:
    await _dead_tenant_probe(auth_db, "suspended")


async def test_probe_offboarded_tenant(auth_db: Conn) -> None:
    await _dead_tenant_probe(auth_db, "offboarded")


async def test_probe_trial_tenant_ok(auth_db: Conn) -> None:
    tenant = add_tenant(auth_db, "trialco", status="trial")
    user = add_user(auth_db, "owner@trialco.test")
    add_member(auth_db, tenant, user, "operator")
    row = await _probe(tenant, user)
    assert row is not None and row.role == "operator"


async def test_list_active_for_user_cross_tenant(auth_db: Conn) -> None:
    from app.repositories.identity import MembershipRepository
    from app.repositories.transactions import identity_session, set_gucs

    world = build_world(auth_db)
    user = world.user("operator@acme.test")
    now = datetime.now(UTC)
    auth_db.execute(
        "UPDATE tenant_memberships SET last_used_at = %s WHERE user_id = %s", [now, user]
    )
    add_member(auth_db, world.tenant("bharat"), user, "reviewer", last_used_at=now + timedelta(1))
    gone = add_tenant(auth_db, "gone", status="suspended")
    add_member(auth_db, gone, user, "owner")
    quiet = add_tenant(auth_db, "quiet")
    add_member(auth_db, quiet, user, "owner", active=False)
    add_member(auth_db, quiet, add_user(auth_db, "keeper@quiet.test"), "owner")

    async with identity_session() as session:
        await set_gucs(session, user_id=user)
        rows = await MembershipRepository(session).list_active_for_user(user)
    assert [(r.tenant_id, r.role) for r in rows] == [
        (world.tenant("bharat"), "reviewer"),
        (world.tenant("acme"), "operator"),
    ]
    assert {r.tenant_name for r in rows} == {"Acme", "Bharat"}

    # without app.user_id the caller sees none of its memberships (RLS, not the WHERE clause)
    async with identity_session() as session:
        assert await MembershipRepository(session).list_active_for_user(user) == []


async def test_by_hash_for_update_locks(auth_db: Conn) -> None:
    from app.repositories.identity import RefreshTokenRepository
    from app.repositories.transactions import identity_session, set_gucs

    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    row_id = _refresh(auth_db, acme, owner, uuid.uuid4(), "locked")
    async with identity_session() as session:
        await set_gucs(session, tenant_id=acme, user_id=owner, token_hash=_hash("locked"))
        row = await RefreshTokenRepository(session).by_hash_for_update(_hash("locked"))
        assert row is not None and row.id == row_id
        with (
            psycopg.connect(libpq_url(_admin_url()), connect_timeout=5) as other,
            pytest.raises(psycopg.errors.LockNotAvailable),
        ):
            other.execute("SELECT 1 FROM refresh_tokens WHERE id = %s FOR UPDATE NOWAIT", [row_id])


def _admin_url() -> str:
    from app.core.config import get_settings

    url = get_settings().alembic_database_url
    assert url is not None
    return url


async def test_refresh_lock_needs_tenant_guc(auth_db: Conn) -> None:
    from app.repositories.identity import RefreshTokenRepository
    from app.repositories.transactions import identity_session, set_gucs

    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    _refresh(auth_db, acme, owner, uuid.uuid4(), "guc")
    async with identity_session() as session:
        # the hash branch alone admits a plain SELECT, but not a locking read
        await set_gucs(session, token_hash=_hash("guc"))
        assert await RefreshTokenRepository(session).by_hash_for_update(_hash("guc")) is None
        visible = await session.execute(
            text("SELECT count(*) FROM refresh_tokens WHERE token_hash = :h"), {"h": _hash("guc")}
        )
        assert visible.scalar_one() == 1
    async with identity_session() as session:
        await set_gucs(session, tenant_id=world.tenant("bharat"), token_hash=_hash("guc"))
        assert await RefreshTokenRepository(session).by_hash_for_update(_hash("guc")) is None
    async with identity_session() as session:
        await set_gucs(session, tenant_id=acme, token_hash=_hash("guc"))
        row = await RefreshTokenRepository(session).by_hash_for_update(_hash("guc"))
        assert row is not None and (row.tenant_id, row.user_id) == (acme, owner)


async def test_revoke_family_counts(auth_db: Conn) -> None:
    from app.repositories.identity import RefreshTokenRepository
    from app.repositories.transactions import identity_session, set_gucs

    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    family, other = uuid.uuid4(), uuid.uuid4()
    for label in ("a", "b"):
        _refresh(auth_db, acme, owner, family, label)
    _refresh(auth_db, acme, owner, family, "c", revoked=True)
    _refresh(auth_db, acme, owner, other, "d")
    async with identity_session() as session:
        await set_gucs(session, tenant_id=acme, user_id=owner)
        repo = RefreshTokenRepository(session)
        assert await repo.revoke_family(acme, family, "logout", expect_live=True) == 2
        assert await repo.revoke_family(acme, family, "logout", expect_live=False) == 0
    rows = auth_db.execute(
        "SELECT family_id, revoked_at IS NOT NULL, revoked_reason FROM refresh_tokens"
    ).fetchall()
    assert sorted((f == family, r, reason) for f, r, reason in rows) == [
        (False, False, None),
        (True, True, "logout"),
        (True, True, "logout"),
        (True, True, "rotated"),
    ]


async def test_revoke_family_zero_rows_raises(auth_db: Conn) -> None:
    from app.core.errors import RowCountError
    from app.repositories.identity import RefreshTokenRepository
    from app.repositories.transactions import identity_session, set_gucs

    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    family = uuid.uuid4()
    _refresh(auth_db, acme, owner, family, "dead", revoked=True)
    _refresh(auth_db, acme, owner, uuid.uuid4(), "live")
    with pytest.raises(RowCountError):
        async with identity_session() as session:
            await set_gucs(session, tenant_id=acme, user_id=owner)
            await RefreshTokenRepository(session).revoke_family(
                acme, family, "logout", expect_live=True
            )
    # a live family under the wrong tenant GUC is 0 rows too (RLS), never a cross-tenant write
    live = auth_db.execute(
        "SELECT family_id FROM refresh_tokens WHERE token_hash = %s", [_hash("live")]
    ).fetchone()
    assert live is not None
    with pytest.raises(RowCountError):
        async with identity_session() as session:
            await set_gucs(session, tenant_id=world.tenant("bharat"), user_id=owner)
            await RefreshTokenRepository(session).revoke_family(
                acme, live[0], "logout", expect_live=True
            )
    revoked = auth_db.execute(
        "SELECT revoked_at FROM refresh_tokens WHERE token_hash = %s", [_hash("live")]
    ).fetchone()
    assert revoked == (None,)


async def test_audit_write_no_returning(auth_db: Conn) -> None:
    from app.core.request_context import request_context
    from app.repositories.identity import AuditRepository
    from app.repositories.transactions import identity_session

    build_world(auth_db)
    with request_context("req-c4-audit"):
        async with identity_session() as session:
            await AuditRepository(session).write(
                tenant_id=None,
                actor_type="system",
                actor_user_id=None,
                action="auth.login_failed",
                entity_type="user",
                entity_id=None,
                after=json.dumps({"email": "x@y.z"}),
                ip="203.0.113.7",
            )
    rows = auth_db.execute(
        "SELECT tenant_id, actor_type, entity_type, after, request_id, host(ip) FROM audit_log"
    ).fetchall()
    # `user` keeps no keys (Q1), so the email never lands in the row
    assert rows == [(None, "system", "user", {}, "req-c4-audit", "203.0.113.7")]


async def test_audit_write_refuses_unregistered_entity(auth_db: Conn) -> None:
    from app.repositories.identity import AuditRepository
    from app.repositories.transactions import identity_session

    with pytest.raises(ValueError, match="AUDITED_ENTITIES"):
        async with identity_session() as session:
            await AuditRepository(session).write(
                tenant_id=None,
                actor_type="system",
                actor_user_id=None,
                action="x",
                entity_type="password",
                entity_id=None,
            )


async def test_identity_scope_not_read_only(auth_db: Conn) -> None:
    from app.db.guard import SCOPE_OK
    from app.repositories.identity import RefreshTokenRepository, UserRepository
    from app.repositories.transactions import identity_session, set_gucs

    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    async with identity_session() as session:
        await set_gucs(session, login_email="OWNER@acme.test")
        user = await UserRepository(session).by_email("Owner@Acme.test")
        assert user is not None and user.id == owner and user.hashed_password
        await set_gucs(session, tenant_id=acme, user_id=owner)
        await RefreshTokenRepository(session).insert(
            tenant_id=acme,
            user_id=owner,
            family_id=uuid.uuid4(),
            token_hash=_hash("new"),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        role = await session.execute(
            text("SELECT current_user, current_setting('transaction_read_only')")
        )
        assert tuple(role.one()) == ("app_rw", "off")
        assert SCOPE_OK not in session.info
    landed = auth_db.execute(
        "SELECT tenant_id, user_id FROM refresh_tokens WHERE token_hash = %s", [_hash("new")]
    ).fetchone()
    assert landed == (acme, owner)


async def test_user_by_email_needs_login_guc(auth_db: Conn) -> None:
    from app.repositories.identity import UserRepository
    from app.repositories.transactions import identity_session, set_gucs

    build_world(auth_db)
    async with identity_session() as session:
        assert await UserRepository(session).by_email("owner@acme.test") is None
        await set_gucs(session, login_email="reviewer@acme.test")
        assert await UserRepository(session).by_email("owner@acme.test") is None


async def test_gucs_reapplied_next_tx(auth_db: Conn) -> None:
    from app.repositories.transactions import identity_session, set_gucs

    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    query = text(
        "SELECT current_setting('app.tenant_id', true), current_setting('app.user_id', true), "
        "current_setting('app.login_email', true), current_setting('app.token_hash', true)"
    )
    async with identity_session() as session:
        await set_gucs(
            session, tenant_id=acme, user_id=owner, login_email="A@B.co", token_hash=_hash("t")
        )
        first = tuple((await session.execute(query)).one())
        await session.commit()
        second = tuple((await session.execute(query)).one())
    assert first == second == (str(acme), str(owner), "a@b.co", _hash("t").hex())


@pytest.mark.parametrize(
    "kwargs",
    [{"tenant_id": "x"}, {"user_id": 1}, {"login_email": "  "}, {"token_hash": b"short"}],
)
async def test_set_gucs_refuses_bad_values(auth_db: Conn, kwargs: dict[str, Any]) -> None:
    from app.repositories.transactions import identity_session, set_gucs

    with pytest.raises(ValueError):
        async with identity_session() as session:
            await set_gucs(session, **kwargs)
