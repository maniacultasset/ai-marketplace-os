"""0005's identity branches as app_rw (DATABASE.md:230, :244, :258, :306-311; §11.2 row 19): `users`
by self, login email and teammates; `tenant_memberships` by own rows across tenants; `tenants` by
the active tenant and the switcher (the caller's active memberships). Each is a named invariant-5
exception, so each also proves what it hides."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


def _ids(conn: Conn, query: str, params: Any = None) -> set[UUID]:
    return {row[0] for row in conn.execute(query, params).fetchall()}


def _email(conn: Conn, user_id: UUID) -> str:
    row = conn.execute("SELECT email FROM users WHERE id = %s", [user_id]).fetchone()
    assert row is not None
    return str(row[0])


def test_rls_branch_users_self(db: Conn) -> None:
    me, other = f.user(db), f.user(db)
    set_gucs(db, user_id=me)
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM users WHERE id IN (%s, %s)", [me, other]) == {me}
        assert db.execute("UPDATE users SET display_name = 'me' WHERE id = %s", [me]).rowcount == 1


def test_rls_branch_users_login_email(db: Conn) -> None:
    me = f.user(db, email="Login.Me@X.test")
    set_gucs(db, login_email="login.me@x.test")
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM users") == {me}
        # the login branch is SELECT-only: the self branch writes, after app.user_id is set
        assert db.execute("UPDATE users SET display_name = 'x' WHERE id = %s", [me]).rowcount == 0


def test_rls_branch_users_login_email_other_hidden(db: Conn) -> None:
    me, other = f.user(db), f.user(db)
    set_gucs(db, login_email=_email(db, me).lower())
    with as_app_rw(db):
        assert other not in _ids(db, "SELECT id FROM users")


def test_rls_branch_users_signup_on_login_email(db: Conn) -> None:
    set_gucs(db, login_email="new.user@x.test")
    with as_app_rw(db):
        db.execute(
            "INSERT INTO users (email, hashed_password) VALUES ('new.user@x.test', 'argon2id-hash')"
        )
        expect_sqlstate(
            db,
            "42501",
            "INSERT INTO users (email, hashed_password) VALUES ('other@x.test', 'argon2id-hash')",
        )


def test_rls_branch_users_teammates(db: Conn) -> None:
    a = f.tenant(db)
    me, mate = f.member(db, a), f.member(db, a, role="reviewer")
    gone = f.member(db, a, role="operator", is_active=False, deactivated_at="2026-01-01")
    set_gucs(db, tenant_id=a, user_id=me)
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM users") == {me, mate, gone}
        # teammates are read-only: only the self branch writes
        assert db.execute("UPDATE users SET display_name = 'x' WHERE id = %s", [mate]).rowcount == 0


def test_rls_branch_users_non_teammate_hidden(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    me, stranger = f.member(db, a), f.member(db, b)
    set_gucs(db, tenant_id=a, user_id=me)
    with as_app_rw(db):
        assert stranger not in _ids(db, "SELECT id FROM users")
    set_gucs(db, tenant_id=None, user_id=None)
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM users WHERE id IN (%s, %s)", [me, stranger]) == set()


def test_rls_branch_memberships_own_rows_cross_tenant(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    me = f.member(db, a)
    f.insert(db, "tenant_memberships", tenant_id=b, user_id=me, role="reviewer")
    set_gucs(db, user_id=me)  # login: no tenant yet
    with as_app_rw(db):
        assert _ids(db, "SELECT tenant_id FROM tenant_memberships") == {a, b}
        # own rows are SELECT-only: a write needs the tenant GUC
        updated = db.execute(
            "UPDATE tenant_memberships SET last_used_at = now() WHERE user_id = %s", [me]
        ).rowcount
        assert updated == 0
    set_gucs(db, tenant_id=a)
    with as_app_rw(db):
        updated = db.execute(
            "UPDATE tenant_memberships SET last_used_at = now() WHERE user_id = %s", [me]
        ).rowcount
        assert updated == 1


def test_rls_branch_memberships_others_hidden(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    me, other_b = f.member(db, a), f.member(db, b)
    set_gucs(db, tenant_id=a, user_id=me)
    with as_app_rw(db):
        assert other_b not in _ids(db, "SELECT user_id FROM tenant_memberships")
        assert _ids(db, "SELECT tenant_id FROM tenant_memberships") == {a}


def test_rls_branch_tenants_own(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    set_gucs(db, tenant_id=a)
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM tenants WHERE id IN (%s, %s)", [a, b]) == {a}
        assert db.execute("UPDATE tenants SET name = 'x' WHERE id = %s", [b]).rowcount == 0
        assert db.execute("UPDATE tenants SET name = 'x' WHERE id = %s", [a]).rowcount == 1


def test_rls_branch_tenants_switcher_active_only(db: Conn) -> None:
    a, b, c = f.tenant(db), f.tenant(db), f.tenant(db)
    me = f.member(db, a)
    f.insert(db, "tenant_memberships", tenant_id=b, user_id=me, role="operator")
    f.member(db, c)  # someone else's tenant
    set_gucs(db, tenant_id=a, user_id=me)
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM tenants WHERE id IN (%s, %s, %s)", [a, b, c]) == {a, b}
        # the switcher is SELECT-only: tenant B is written only under B's GUC
        assert db.execute("UPDATE tenants SET name = 'x' WHERE id = %s", [b]).rowcount == 0


def test_rls_branch_tenants_inactive_hidden(db: Conn) -> None:
    a, c = f.tenant(db), f.tenant(db)
    me = f.member(db, a)
    f.insert(
        db,
        "tenant_memberships",
        tenant_id=c,
        user_id=me,
        role="operator",
        is_active=False,
        deactivated_at="2026-01-01",
    )
    set_gucs(db, tenant_id=a, user_id=me)
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM tenants WHERE id IN (%s, %s)", [a, c]) == {a}
