"""0005's token branches and `jr_by_id` as app_rw (DATABASE.md:271, :283, :950; §11.2 row 19): a
refresh token, an invitation or an OAuth state is found by its hash (`app.token_hash`) before any
tenant GUC exists, a refresh token also by its owner (`app.user_id`), and a job by its id
(`app.job_id`). Every branch is SELECT-only: a write or a locking read still needs the tenant."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from . import factories as f
from . import ops_support as ops
from .integrity_support import as_app_rw, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


@dataclass(frozen=True)
class Side:
    tenant_id: UUID
    user_id: UUID


def _side(conn: Conn) -> Side:
    tenant_id = f.tenant(conn)
    return Side(tenant_id, f.member(conn, tenant_id))


def _digest(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def _token(conn: Conn, side: Side, label: str, user_id: UUID | None = None) -> UUID:
    return f.insert(  # type: ignore[no-any-return]
        conn,
        "refresh_tokens",
        tenant_id=side.tenant_id,
        user_id=user_id or side.user_id,
        token_hash=_digest(label),
        expires_at="2099-01-01T00:00:00Z",
        family_id=uuid4(),
    )


def _ids(conn: Conn, query: Any, params: Any = None) -> set[UUID]:
    return {row[0] for row in conn.execute(query, params).fetchall()}


def test_rls_branch_refresh_hash(db: Conn) -> None:
    a, b = _side(db), _side(db)
    mine = _token(db, b, "refresh-mine")
    _token(db, a, "refresh-other")
    set_gucs(db, token_hash=_digest("refresh-mine").hex())  # no tenant, no user yet
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM refresh_tokens") == {mine}
        row = db.execute("SELECT tenant_id, user_id FROM refresh_tokens").fetchone()
        assert row == (b.tenant_id, b.user_id)


def test_rls_branch_refresh_wrong_hash_hidden(db: Conn) -> None:
    a = _side(db)
    _token(db, a, "refresh-real")
    set_gucs(db, token_hash=_digest("refresh-guess").hex())
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM refresh_tokens") == set()
    set_gucs(db, token_hash=None)
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM refresh_tokens") == set()


def test_rls_branch_refresh_own_rows(db: Conn) -> None:
    a, b = _side(db), _side(db)
    f.insert(db, "tenant_memberships", tenant_id=b.tenant_id, user_id=a.user_id, role="reviewer")
    in_a = _token(db, a, "own-a")
    in_b = _token(db, b, "own-b", user_id=a.user_id)
    _token(db, b, "theirs")  # b's owner
    set_gucs(db, user_id=a.user_id)
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM refresh_tokens") == {in_a, in_b}


def test_rls_branch_invitation_hash(db: Conn) -> None:
    a, b = _side(db), _side(db)
    mine = f.insert(
        db,
        "invitations",
        tenant_id=b.tenant_id,
        email="invitee@x.test",
        role="operator",
        token_hash=_digest("invite-mine"),
        invited_by_user_id=b.user_id,
        expires_at="2099-01-01T00:00:00Z",
    )
    f.insert(
        db,
        "invitations",
        tenant_id=a.tenant_id,
        email="invitee@x.test",
        role="operator",
        token_hash=_digest("invite-other"),
        invited_by_user_id=a.user_id,
        expires_at="2099-01-01T00:00:00Z",
    )
    set_gucs(db, token_hash=_digest("invite-mine").hex())
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM invitations") == {mine}


def test_rls_branch_oauth_state_hash(db: Conn) -> None:
    a, b = _side(db), _side(db)
    state: dict[str, Any] = {"marketplace": "amazon", "expires_at": "2099-01-01T00:00:00Z"}
    mine = f.insert(
        db,
        "oauth_states",
        tenant_id=b.tenant_id,
        user_id=b.user_id,
        state_hash=_digest("state-mine"),
        **state,
    )
    f.insert(
        db,
        "oauth_states",
        tenant_id=a.tenant_id,
        user_id=a.user_id,
        state_hash=_digest("state-other"),
        **state,
    )
    set_gucs(db, token_hash=_digest("state-mine").hex())  # the callback has no tenant yet
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM oauth_states") == {mine}


def test_rls_branch_jr_by_id(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    job = ops.job_run(db, b)
    ops.job_run(db, a)
    set_gucs(db, job_id=job)  # the worker: payload carries only job_run_id
    with as_app_rw(db):
        assert db.execute("SELECT id, tenant_id FROM job_runs").fetchall() == [(job, b)]


def test_rls_branch_jr_by_id_other_hidden(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    job, other_a, other_b = ops.job_run(db, b), ops.job_run(db, a), ops.job_run(db, b)
    set_gucs(db, job_id=job)
    with as_app_rw(db):
        assert _ids(db, "SELECT id FROM job_runs") == {job}
        assert _ids(db, "SELECT id FROM job_runs WHERE id IN (%s, %s)", [other_a, other_b]) == set()


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("refresh_tokens", "revoked_at"),
        ("invitations", "revoked_at"),
        ("oauth_states", "expires_at"),
        ("job_runs", "queued_at"),
    ],
)
def test_rls_branch_hash_branches_cannot_update(db: Conn, table: str, column: str) -> None:
    """With every branch GUC naming the row but no tenant GUC, UPDATE, DELETE and SELECT … FOR
    UPDATE find nothing (Postgres checks them against the UPDATE/DELETE policies, CODE-PLAN Q10)."""
    side = _side(db)
    label = f"cannot-update-{table}"
    if table == "refresh_tokens":
        row = _token(db, side, label)
    elif table == "invitations":
        row = f.insert(
            db,
            "invitations",
            tenant_id=side.tenant_id,
            email="invitee@x.test",
            role="operator",
            token_hash=_digest(label),
            invited_by_user_id=side.user_id,
            expires_at="2099-01-01T00:00:00Z",
        )
    elif table == "oauth_states":
        row = f.insert(
            db,
            "oauth_states",
            tenant_id=side.tenant_id,
            user_id=side.user_id,
            state_hash=_digest(label),
            marketplace="amazon",
            expires_at="2099-01-01T00:00:00Z",
        )
    else:
        row = ops.job_run(db, side.tenant_id)
    set_gucs(db, token_hash=_digest(label).hex(), user_id=side.user_id, job_id=row)
    ident = sql.Identifier(table)
    with as_app_rw(db):
        assert _ids(db, sql.SQL("SELECT id FROM {}").format(ident)) == {row}
        update = sql.SQL("UPDATE {} SET {} = now() WHERE id = %s").format(
            ident, sql.Identifier(column)
        )
        assert db.execute(update, [row]).rowcount == 0
        locked = sql.SQL("SELECT id FROM {} WHERE id = %s FOR UPDATE").format(ident)
        assert db.execute(locked, [row]).fetchall() == []
        delete = sql.SQL("DELETE FROM {} WHERE id = %s").format(ident)
        # 0007: the callback consumes its state row by hash (oauth_states_hash_delete); a
        # savepoint keeps the row for the tenant UPDATE below
        with db.transaction(force_rollback=True):
            deleted = db.execute(delete, [row]).rowcount
        assert deleted == (1 if table == "oauth_states" else 0)
    set_gucs(db, tenant_id=side.tenant_id)
    with as_app_rw(db):
        assert db.execute(update, [row]).rowcount == 1
