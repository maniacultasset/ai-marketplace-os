"""DPP-1 (0011; ADR-0021): the four per-user MFA tables and the refresh-token session proof as the
raw `app_rw` login, with no repository in the way. User A reads and writes none of B's factors,
recovery codes, challenges or password history; `app.challenge_hash` names one challenge and no
other row; the read-only roles see no secret or hash column; `refresh_tokens.amr` stays
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
TABLES = ("user_mfa_factors", "user_recovery_codes", "mfa_challenges", "user_password_history")


def _digest(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


@dataclass(frozen=True)
class MfaWorld:
    a: UUID
    b: UUID
    tenant_a: UUID
    tenant_b: UUID


def _seed_user(conn: Conn, email: str, tenant: UUID, label: str) -> UUID:
    row = conn.execute(
        "INSERT INTO users (email, hashed_password) VALUES (%s, %s) RETURNING id", [email, HASH]
    ).fetchone()
    assert row is not None
    user: UUID = row[0]
    conn.execute(
        "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES (%s, %s, 'owner')",
        [tenant, user],
    )
    conn.execute(
        "INSERT INTO user_mfa_factors (user_id, secret_blob, key_version) VALUES (%s, %s, 'local-1')",
        [user, b"MFA1-" + label.encode()],
    )
    conn.execute(
        "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
        [user, _digest(f"rc-{label}")],
    )
    conn.execute(
        "INSERT INTO mfa_challenges (user_id, challenge_hash, purpose, amr, expires_at) "
        "VALUES (%s, %s, 'login', ARRAY['pwd'], now() + interval '5 minutes')",
        [user, _digest(f"ch-{label}")],
    )
    conn.execute(
        "INSERT INTO user_password_history (user_id, hashed_password) VALUES (%s, %s)",
        [user, HASH],
    )
    conn.execute(
        "INSERT INTO refresh_tokens (tenant_id, user_id, family_id, token_hash, expires_at, amr, "
        "mfa_at) VALUES (%s, %s, %s, %s, now() + interval '1 day', ARRAY['pwd','otp'], now())",
        [tenant, user, uuid4(), _digest(f"rt-{label}")],
    )
    return user


@pytest.fixture(scope="module")
def world(clean_module_admin_conn: Conn) -> MfaWorld:
    conn = clean_module_admin_conn
    tenants = []
    for slug in ("acme", "bharat"):
        row = conn.execute(
            "INSERT INTO tenants (name, slug, status) VALUES (%s, %s, 'active') RETURNING id",
            [slug.title(), slug],
        ).fetchone()
        assert row is not None
        tenants.append(row[0])
    a = _seed_user(conn, "a@acme.test", tenants[0], "a")
    b = _seed_user(conn, "b@bharat.test", tenants[1], "b")
    return MfaWorld(a, b, tenants[0], tenants[1])


@contextmanager
def as_app(conn: Conn, **gucs: Any) -> Iterator[Conn]:
    with conn.transaction(force_rollback=True):
        for name, value in gucs.items():
            conn.execute("SELECT set_config(%s, %s, true)", [f"app.{name}", str(value)])
        yield conn


def _count(conn: Conn, table: str, user: UUID) -> int:
    query = f"SELECT count(*) FROM {table} WHERE user_id = %s"  # noqa: S608 - constant names
    row = conn.execute(query, [user]).fetchone()
    assert row is not None
    return int(row[0])


def test_user_a_cannot_read_or_write_b_factors_codes_challenges_history(
    app_conn: Conn, world: MfaWorld
) -> None:
    with as_app(app_conn, user_id=world.a):
        for table in TABLES:
            assert _count(app_conn, table, world.a) == 1, table
            assert _count(app_conn, table, world.b) == 0, table
            deleted = app_conn.execute(
                f"DELETE FROM {table} WHERE user_id = %s", [world.b]  # noqa: S608
            )
            assert deleted.rowcount == 0, table
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute(
                "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
                [world.b, _digest("planted")],
            )


def test_no_guc_sees_nothing(app_conn: Conn, world: MfaWorld) -> None:
    with as_app(app_conn):
        for table in TABLES:
            row = app_conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
            assert row == (0,), table


def test_challenge_hash_guc_scopes_one_row(app_conn: Conn, world: MfaWorld) -> None:
    with as_app(app_conn, challenge_hash=_digest("ch-a").hex()):
        rows = app_conn.execute("SELECT user_id FROM mfa_challenges").fetchall()
        assert rows == [(world.a,)]
        for table in ("user_mfa_factors", "user_recovery_codes", "user_password_history"):
            row = app_conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
            assert row == (0,), table


def test_platform_ro_columns_only(admin_conn: Conn, world: MfaWorld) -> None:
    with admin_conn.transaction(force_rollback=True):
        admin_conn.execute("SET LOCAL ROLE app_platform_ro")
        rows = admin_conn.execute("SELECT user_id, status FROM user_mfa_factors").fetchall()
        assert len(rows) == 2
    for query in (
        "SELECT secret_blob FROM user_mfa_factors",
        "SELECT code_hash FROM user_recovery_codes",
        "SELECT challenge_hash FROM mfa_challenges",
        "SELECT hashed_password FROM user_password_history",
    ):
        with admin_conn.transaction(force_rollback=True):
            admin_conn.execute("SET LOCAL ROLE app_platform_ro")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                admin_conn.execute(query)


def test_refresh_tokens_amr_column_tenant_scoped(app_conn: Conn, world: MfaWorld) -> None:
    with as_app(app_conn, tenant_id=world.tenant_a, user_id=world.a):
        rows = app_conn.execute("SELECT user_id, amr FROM refresh_tokens").fetchall()
        assert rows == [(world.a, ["pwd", "otp"])]
        changed = app_conn.execute(
            "UPDATE refresh_tokens SET amr = ARRAY['pwd'] WHERE user_id = %s", [world.b]
        )
        assert changed.rowcount == 0
