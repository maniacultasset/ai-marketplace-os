"""`tenant_memberships` and `invitations` (DATABASE.md §5.1; ADR-0006)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

# Every `→ m` column here; test_0001_catalog checks the union of these lists against pg_constraint.
CROSS_TENANT_FK_COLUMNS = [
    "tenant_memberships.invited_by_user_id",
    "invitations.invited_by_user_id",
]

INSERT_MEMBERSHIP = "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES (%s, %s, %s)"


def _digest() -> bytes:
    return hashlib.sha256(secrets.token_bytes(32)).digest()


def _expires() -> datetime:
    return datetime.now(UTC) + timedelta(days=7)


def invite(conn: Conn, tenant_id: UUID, email: str = "new@x.test", **columns: Any) -> UUID:
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "email": email,
        "role": "operator",
        "token_hash": _digest(),
        "expires_at": _expires(),
    }
    return f.insert(conn, "invitations", **{**values, **columns})  # type: ignore[no-any-return]


def test_memberships_one_per_tenant_user(db: Conn) -> None:
    tenant_id = f.tenant(db)
    user_id = f.member(db, tenant_id)
    expect_sqlstate(
        db,
        "23505",
        INSERT_MEMBERSHIP,
        (tenant_id, user_id, "operator"),
        constraint="uq_tenant_memberships_tenant_id_user_id",
    )


@pytest.mark.parametrize("role", ["owner", "operator", "reviewer", "admin"])
def test_memberships_role_accepted(db: Conn, role: str) -> None:
    f.member(db, f.tenant(db), role=role)


@pytest.mark.parametrize("role", ["Owner", "viewer", ""])
def test_memberships_role_vocab(db: Conn, role: str) -> None:
    tenant_id = f.tenant(db)
    expect_sqlstate(
        db,
        "23514",
        INSERT_MEMBERSHIP,
        (tenant_id, f.user(db), role),
        constraint="ck_tenant_memberships_role",
    )


def test_memberships_same_user_two_tenants_ok(db: Conn) -> None:
    user_id = f.user(db)
    for _ in range(2):
        db.execute(INSERT_MEMBERSHIP, (f.tenant(db), user_id, "owner"))


def test_memberships_defaults(db: Conn) -> None:
    tenant_id = f.tenant(db)
    user_id = f.member(db, tenant_id)
    row = db.execute(
        "SELECT is_active FROM tenant_memberships WHERE tenant_id = %s AND user_id = %s",
        (tenant_id, user_id),
    ).fetchone()
    assert row == (True,)


def test_memberships_user_delete_restricted(db: Conn) -> None:
    user_id = f.member(db, f.tenant(db))
    action = db.execute(
        "SELECT confdeltype FROM pg_constraint WHERE conname = 'fk_tenant_memberships_user_id_users'"
    ).fetchone()
    assert action == ("r",)  # RESTRICT, not NO ACTION (DATABASE.md §1 exceptions)
    expect_sqlstate(
        db,
        "23503",
        "DELETE FROM users WHERE id = %s",
        (user_id,),
        constraint="fk_tenant_memberships_user_id_users",
    )


def test_memberships_invited_by_same_tenant_ok(db: Conn) -> None:
    tenant_id = f.tenant(db)
    inviter = f.member(db, tenant_id)
    f.member(db, tenant_id, invited_by_user_id=inviter)
    invite(db, tenant_id, invited_by_user_id=inviter)


@pytest.mark.parametrize("column", CROSS_TENANT_FK_COLUMNS)
def test_0001_cross_tenant_fk_identity(db: Conn, column: str) -> None:
    table, name = column.split(".")
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    outsider = f.member(db, tenant_b)  # a member of B only
    if table == "tenant_memberships":
        query = (
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, invited_by_user_id) "
            "VALUES (%s, %s, 'operator', %s)"
        )
        params: tuple[Any, ...] = (tenant_a, f.user(db), outsider)
    else:
        query = (
            "INSERT INTO invitations (tenant_id, email, role, token_hash, expires_at, "
            "invited_by_user_id) VALUES (%s, 'x@x.test', 'operator', %s, %s, %s)"
        )
        params = (tenant_a, _digest(), _expires(), outsider)
    expect_sqlstate(db, "23503", query, params, constraint=f"fk_{table}_{name}")


def test_invitations_one_pending_per_email(db: Conn) -> None:
    tenant_id = f.tenant(db)
    first = invite(db, tenant_id)
    with pytest.raises(psycopg.errors.UniqueViolation) as info, db.transaction():
        invite(db, tenant_id)
    assert info.value.diag.constraint_name == "uq_invitations_pending"
    invite(db, f.tenant(db))  # another tenant may invite the same email
    db.execute("UPDATE invitations SET revoked_at = now() WHERE id = %s", (first,))
    second = invite(db, tenant_id)
    accepted_by = f.user(db)
    db.execute(
        "UPDATE invitations SET accepted_at = now(), accepted_user_id = %s WHERE id = %s",
        (accepted_by, second),
    )
    invite(db, tenant_id)


@pytest.mark.parametrize("size", [31, 33, 0])
def test_invitations_token_hash_32_bytes(db: Conn, size: int) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        invite(db, f.tenant(db), token_hash=secrets.token_bytes(size))
    assert info.value.diag.constraint_name == "ck_invitations_token_hash"


def test_invitations_token_hash_32_bytes_ok(db: Conn) -> None:
    invite(db, f.tenant(db), token_hash=secrets.token_bytes(32))


def test_invitations_token_hash_unique(db: Conn) -> None:
    digest = _digest()
    invite(db, f.tenant(db), token_hash=digest)
    with pytest.raises(psycopg.errors.UniqueViolation) as info, db.transaction():
        invite(db, f.tenant(db), token_hash=digest)
    assert info.value.diag.constraint_name == "uq_invitations_token_hash"


def test_invitations_accepted_revoked_exclusive(db: Conn) -> None:
    invitation = invite(db, f.tenant(db))
    expect_sqlstate(
        db,
        "23514",
        "UPDATE invitations SET accepted_at = now(), accepted_user_id = %s, revoked_at = now() "
        "WHERE id = %s",
        (f.user(db), invitation),
        constraint="ck_invitations_accepted_revoked",
    )


@pytest.mark.parametrize("email", [" A@x.test", "A@x.test", "a@x.test ", "ax.test", ""])
def test_invitations_email_normalized(db: Conn, email: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        invite(db, f.tenant(db), email=email)
    assert info.value.diag.constraint_name == "ck_invitations_email"


@pytest.mark.parametrize(
    ("column", "value"), [("role", "viewer"), ("role", "Owner"), ("expires_at", None)]
)
def test_invitations_role_and_expiry(db: Conn, column: str, value: Any) -> None:
    with pytest.raises(psycopg.errors.IntegrityError) as info, db.transaction():
        invite(db, f.tenant(db), **{column: value})
    wanted = "23502" if value is None else "23514"
    assert info.value.sqlstate == wanted
