"""ADR-0023 (0013): trusted browsers as the raw `app_rw` login, with no repository in the way.
User A reads, rotates and deletes none of B's trusted browsers and cannot plant one for B; no GUC
sees nothing; the read-only roles never read a hash; the refresh rows' browser fields stay
tenant-scoped."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

Conn = psycopg.Connection[tuple[Any, ...]]
HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA"


def _digest(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


@dataclass(frozen=True)
class DeviceWorld:
    a: UUID
    b: UUID
    tenant_a: UUID
    tenant_b: UUID
    device_b: UUID


def _seed_user(conn: Conn, email: str, tenant: UUID, label: str) -> tuple[UUID, UUID]:
    row = conn.execute(
        "INSERT INTO users (email, hashed_password) VALUES (%s, %s) RETURNING id", [email, HASH]
    ).fetchone()
    assert row is not None
    user: UUID = row[0]
    conn.execute(
        "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES (%s, %s, 'owner')",
        [tenant, user],
    )
    device = conn.execute(
        "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, expires_at) "
        "VALUES (%s, %s, 'otp', 'Chrome on macOS', now(), now() + interval '30 days') "
        "RETURNING id",
        [user, _digest(f"dt-{label}")],
    ).fetchone()
    assert device is not None
    conn.execute(
        "INSERT INTO refresh_tokens (tenant_id, user_id, family_id, token_hash, expires_at, amr, "
        "mfa_at, device_id, device_auth_at) VALUES (%s, %s, %s, %s, now() + interval '1 day', "
        "ARRAY['pwd','otp'], now() - interval '1 day', %s, now())",
        [tenant, user, uuid4(), _digest(f"rt-{label}"), device[0]],
    )
    return user, device[0]


@pytest.fixture(scope="module")
def world(clean_module_admin_conn: Conn) -> DeviceWorld:
    conn = clean_module_admin_conn
    tenants = []
    for slug in ("acme", "bharat"):
        row = conn.execute(
            "INSERT INTO tenants (name, slug, status) VALUES (%s, %s, 'active') RETURNING id",
            [slug.title(), slug],
        ).fetchone()
        assert row is not None
        tenants.append(row[0])
    a, _ = _seed_user(conn, "a@acme.test", tenants[0], "a")
    b, device_b = _seed_user(conn, "b@bharat.test", tenants[1], "b")
    return DeviceWorld(a, b, tenants[0], tenants[1], device_b)


@contextmanager
def as_app(conn: Conn, **gucs: Any) -> Iterator[Conn]:
    with conn.transaction(force_rollback=True):
        for name, value in gucs.items():
            conn.execute("SELECT set_config(%s, %s, true)", [f"app.{name}", str(value)])
        yield conn


def _count(conn: Conn, user: UUID) -> int:
    row = conn.execute("SELECT count(*) FROM trusted_devices WHERE user_id = %s", [user]).fetchone()
    assert row is not None
    return int(row[0])


def test_user_a_cannot_read_rotate_or_delete_b_browsers(app_conn: Conn, world: DeviceWorld) -> None:
    with as_app(app_conn, user_id=world.a):
        assert _count(app_conn, world.a) == 1
        assert _count(app_conn, world.b) == 0
        # matching B's token by its hash finds nothing under A
        found = app_conn.execute(
            "SELECT count(*) FROM trusted_devices WHERE token_hash = %s", [_digest("dt-b")]
        ).fetchone()
        assert found == (0,)
        rotated = app_conn.execute(
            "UPDATE trusted_devices SET token_hash = %s, prev_token_hash = token_hash "
            "WHERE id = %s",
            [_digest("stolen"), world.device_b],
        )
        assert rotated.rowcount == 0
        deleted = app_conn.execute("DELETE FROM trusted_devices WHERE id = %s", [world.device_b])
        assert deleted.rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute(
                "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, "
                "expires_at) VALUES (%s, %s, 'otp', 'x', now(), now() + interval '1 day')",
                [world.b, _digest("planted")],
            )


def test_no_guc_sees_nothing(app_conn: Conn, world: DeviceWorld) -> None:
    with as_app(app_conn):
        row = app_conn.execute("SELECT count(*) FROM trusted_devices").fetchone()
        assert row == (0,)
        assert app_conn.execute("DELETE FROM trusted_devices").rowcount == 0


def test_platform_ro_never_reads_a_hash(admin_conn: Conn, world: DeviceWorld) -> None:
    with admin_conn.transaction(force_rollback=True):
        admin_conn.execute("SET LOCAL ROLE app_platform_ro")
        rows = admin_conn.execute(
            "SELECT user_id, label, expires_at FROM trusted_devices"
        ).fetchall()
        assert len(rows) == 2
    for query in (
        "SELECT token_hash FROM trusted_devices",
        "SELECT prev_token_hash FROM trusted_devices",
    ):
        with admin_conn.transaction(force_rollback=True):
            admin_conn.execute("SET LOCAL ROLE app_platform_ro")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                admin_conn.execute(query)


def test_refresh_device_fields_stay_tenant_scoped(app_conn: Conn, world: DeviceWorld) -> None:
    with as_app(app_conn, tenant_id=world.tenant_a, user_id=world.a):
        rows = app_conn.execute("SELECT user_id FROM refresh_tokens WHERE device_id IS NOT NULL")
        assert rows.fetchall() == [(world.a,)]
        changed = app_conn.execute(
            "UPDATE refresh_tokens SET revoked_at = now(), revoked_reason = 'device_removed' "
            "WHERE device_id = %s",
            [world.device_b],
        )
        assert changed.rowcount == 0
