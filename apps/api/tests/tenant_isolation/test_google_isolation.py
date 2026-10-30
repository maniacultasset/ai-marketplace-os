"""ADR-0020 (0012): Google sign-in's data as the raw `app_rw` login, with no repository in the way.
`login_flows` answers only the row named by `app.token_hash` (never another browser's flow, never
an UPDATE); user A can neither read nor change B's linked Google account, and cannot link,
unlink or create one except through the definer functions, each only for its own GUC."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
import pytest

Conn = psycopg.Connection[tuple[Any, ...]]
HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA"


def _digest(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


@dataclass(frozen=True)
class GoogleIsoWorld:
    a: UUID
    b: UUID


def _seed(conn: Conn, email: str, tenant: UUID, label: str) -> UUID:
    row = conn.execute(
        "INSERT INTO users (email, hashed_password, google_sub) VALUES (%s, %s, %s) RETURNING id",
        [email, HASH, f"sub-{label}"],
    ).fetchone()
    assert row is not None
    user: UUID = row[0]
    conn.execute(
        "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES (%s, %s, 'owner')",
        [tenant, user],
    )
    conn.execute(
        "INSERT INTO login_flows (phase, flow_hash, binding_hash, sub, email, expires_at) "
        "VALUES ('link', %s, %s, %s, %s, now() + interval '10 minutes')",
        [_digest(f"flow-{label}"), _digest(f"bind-{label}"), f"sub-{label}", email],
    )
    return user


@pytest.fixture(scope="module")
def world(clean_module_admin_conn: Conn) -> GoogleIsoWorld:
    conn = clean_module_admin_conn
    tenants = []
    for slug in ("acme", "bharat"):
        row = conn.execute(
            "INSERT INTO tenants (name, slug, status) VALUES (%s, %s, 'active') RETURNING id",
            [slug.title(), slug],
        ).fetchone()
        assert row is not None
        tenants.append(row[0])
    a = _seed(conn, "a@acme.test", tenants[0], "a")
    b = _seed(conn, "b@bharat.test", tenants[1], "b")
    return GoogleIsoWorld(a, b)


@contextmanager
def as_app(conn: Conn, **gucs: Any) -> Iterator[Conn]:
    with conn.transaction(force_rollback=True):
        for name, value in gucs.items():
            conn.execute("SELECT set_config(%s, %s, true)", [f"app.{name}", str(value)])
        yield conn


def test_no_guc_sees_no_flow(app_conn: Conn, world: GoogleIsoWorld) -> None:
    with as_app(app_conn):
        assert app_conn.execute("SELECT count(*) FROM login_flows").fetchone() == (0,)
        deleted = app_conn.execute("DELETE FROM login_flows")
        assert deleted.rowcount == 0


def test_token_hash_names_one_flow(app_conn: Conn, world: GoogleIsoWorld) -> None:
    with as_app(app_conn, token_hash=_digest("flow-a").hex()):
        rows = app_conn.execute("SELECT sub FROM login_flows").fetchall()
        assert rows == [("sub-a",)]
        deleted = app_conn.execute("DELETE FROM login_flows WHERE sub = 'sub-b'")
        assert deleted.rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute("UPDATE login_flows SET sub = 'stolen'")


def test_user_a_cannot_read_or_change_b_google_account(
    app_conn: Conn, world: GoogleIsoWorld
) -> None:
    with as_app(app_conn, user_id=world.a):
        rows = app_conn.execute("SELECT id, google_sub FROM users").fetchall()
        assert rows == [(world.a, "sub-a")]
        assert app_conn.execute("SELECT app_unlink_google_sub(%s)", [world.b]).fetchone() == (
            False,
        )
        assert app_conn.execute(
            "SELECT app_link_google_sub(%s, 'sub-new')", [world.b]
        ).fetchone() == (False,)
    with as_app(app_conn, user_id=world.a), pytest.raises(psycopg.Error) as info:
        app_conn.execute("UPDATE users SET google_sub = NULL WHERE id = %s", [world.a])
    assert info.value.sqlstate == "GRD01"


def test_google_user_lookup_names_only_an_id(app_conn: Conn, world: GoogleIsoWorld) -> None:
    with as_app(app_conn):
        found = app_conn.execute("SELECT app_google_user('sub-b')").fetchone()
        assert found == (world.b,)
        # the id alone opens nothing: without app.user_id the row stays invisible
        assert app_conn.execute(
            "SELECT count(*) FROM users WHERE id = %s", [world.b]
        ).fetchone() == (0,)


def test_applicant_created_only_for_the_login_email(app_conn: Conn, world: GoogleIsoWorld) -> None:
    with as_app(app_conn, login_email="new@gmail.com"), pytest.raises(psycopg.Error) as info:
        app_conn.execute(
            "SELECT app_create_google_applicant('a@acme.test', 'sub-x', 'n', 'b', 'o', NULL, NULL)"
        )
    assert info.value.sqlstate == "GRD01"
