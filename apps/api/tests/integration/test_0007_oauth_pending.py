"""Revision 0007_oauth_pending (S15 T5; DATABASE.md §5.4 oauth_states): the callback's pending
store. Six nullable columns on `oauth_states` hold a sealed token pair between the public callback
and the authenticated confirm, three CHECKs keep a row either a state row (no token) or a complete
pending row (token, key version and seller, no PKCE verifier), and the hash branch is rebuilt as a
SELECT and a DELETE policy that both require `token_enc IS NULL`, so the JWT-less callback can
consume a state row and never sees or deletes a pending row. The downgrade restores 0005's
SELECT-only `oauth_states_hash` verbatim."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import as_app_rw, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

MakeConfig = Callable[[Path | None], Config]
Conn = psycopg.Connection[tuple[Any, ...]]
REVISION = "0007_oauth_pending"
PREVIOUS = "0005_rls"
NEW_COLUMNS = {"token_enc", "kms_key_version", "seller_id", "region", "scopes", "token_expires_at"}
CHECKS = {
    "ck_oauth_states_pending_complete",
    "ck_oauth_states_pending_no_verifier",
    "ck_oauth_states_pending_bounds",
}


def _digest(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def _world(conn: Conn) -> tuple[uuid.UUID, uuid.UUID]:
    """One tenant and one owner, inserted as the admin login."""
    tenant = conn.execute(
        "INSERT INTO tenants (name, slug, status) VALUES ('Acme', %s, 'active') RETURNING id",
        [f"acme-{uuid.uuid4().hex[:8]}"],
    ).fetchone()
    user = conn.execute(
        "INSERT INTO users (email, hashed_password) VALUES (%s, 'h') RETURNING id",
        [f"{uuid.uuid4().hex}@x.test"],
    ).fetchone()
    assert tenant is not None and user is not None
    conn.execute(
        "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES (%s, %s, 'owner')",
        [tenant[0], user[0]],
    )
    return tenant[0], user[0]


def _state(conn: Conn, tenant: uuid.UUID, user: uuid.UUID, label: str, **extra: Any) -> uuid.UUID:
    columns: dict[str, Any] = {
        "tenant_id": tenant,
        "user_id": user,
        "state_hash": _digest(label),
        "marketplace": "amazon",
        "expires_at": "2099-01-01T00:00:00Z",
        **extra,
    }
    names = ", ".join(columns)
    marks = ", ".join(["%s"] * len(columns))
    row = conn.execute(
        f"INSERT INTO oauth_states ({names}) VALUES ({marks}) RETURNING id",  # noqa: S608
        list(columns.values()),
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


PENDING: dict[str, Any] = {
    "token_enc": b"MKT1" + b"\x00" * 100,
    "kms_key_version": "local-1",
    "seller_id": "A1SELLER",
    "region": "IN",
    "scopes": ["sellingpartnerapi::notifications"],
    "token_expires_at": "2099-01-01T00:00:00Z",
}


def _policies(conn: psycopg.Connection[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT policyname, permissive, cmd, roles::text, qual, with_check FROM pg_policies "
        "WHERE tablename = 'oauth_states' ORDER BY 1"
    ).fetchall()


def _columns(conn: psycopg.Connection[tuple[Any, ...]]) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'oauth_states'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def test_0007_up_down_up(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        before_policies, before_columns = _policies(conn), _columns(conn)
    assert [p[0] for p in before_policies] == [
        "oauth_states_hash",
        "t_migrator",
        "t_platform",
        "t_tenant",
    ]
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _columns(conn) == before_columns | NEW_COLUMNS
        names = [p[0] for p in _policies(conn)]
        assert "oauth_states_hash" not in names
        assert {"oauth_states_hash_select", "oauth_states_hash_delete"} <= set(names)
        tenant, user = _world(conn)
        _state(conn, tenant, user, "state-row")
        _state(conn, tenant, user, "pending-row", **PENDING)
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _columns(conn) == before_columns
        assert _policies(conn) == before_policies
        # the pending row is gone (a 0005-era consume would accept it as a state row)
        hashes = {bytes(r[0]) for r in conn.execute("SELECT state_hash FROM oauth_states")}
        assert hashes == {_digest("state-row")}
    command.upgrade(cfg, REVISION)
    command.check(cfg)


def test_0007_down_restores_s10_hash_policy(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        snapshot = _policies(conn)
    command.upgrade(cfg, REVISION)
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _policies(conn) == snapshot


def test_0007_alembic_check_no_drift(alembic_env: MakeConfig) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_mig_single_head() -> None:
    from app.db.migrations import alembic_config

    assert ScriptDirectory.from_config(alembic_config()).get_heads() == [REVISION]


def test_0007_down_revision_is_ledger_head(repo_root: Path) -> None:
    """The revision's parent is the ledger row before its own (CANON.md migration ledger)."""
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config()).get_revision(REVISION)
    assert script is not None and script.down_revision == PREVIOUS
    ledger = [
        line.split("`")[1]
        for line in (repo_root / "docs/canon/CANON.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| `00") and "_" in line.split("`")[1]
    ]
    assert REVISION in ledger
    assert ledger[ledger.index(REVISION) - 1] == PREVIOUS


def test_0007_checks_present(db: Conn) -> None:
    rows = db.execute(
        "SELECT conname, convalidated FROM pg_constraint "
        "WHERE conrelid = 'oauth_states'::regclass AND contype = 'c'"
    ).fetchall()
    found = {str(r[0]): bool(r[1]) for r in rows}
    assert set(found) >= CHECKS
    assert all(found[name] for name in CHECKS)


REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "token_without_version": (
        {**PENDING, "kms_key_version": None},
        "ck_oauth_states_pending_complete",
    ),
    "token_without_seller": ({**PENDING, "seller_id": None}, "ck_oauth_states_pending_complete"),
    "version_without_token": (
        {"kms_key_version": "local-1"},
        "ck_oauth_states_pending_complete",
    ),
    "pending_with_verifier": (
        {**PENDING, "code_verifier": "v" * 43},
        "ck_oauth_states_pending_no_verifier",
    ),
    "seller_65": ({**PENDING, "seller_id": "A" * 65}, "ck_oauth_states_pending_bounds"),
    "seller_slash": ({**PENDING, "seller_id": "A1/../x"}, "ck_oauth_states_pending_bounds"),
    "seller_empty": ({**PENDING, "seller_id": ""}, "ck_oauth_states_pending_bounds"),
    "token_16385": ({**PENDING, "token_enc": b"x" * 16385}, "ck_oauth_states_pending_bounds"),
    "key_version_513": (
        {**PENDING, "kms_key_version": "v" * 513},
        "ck_oauth_states_pending_bounds",
    ),
    "region_33": ({**PENDING, "region": "r" * 33}, "ck_oauth_states_pending_bounds"),
    "scopes_65": ({**PENDING, "scopes": ["s"] * 65}, "ck_oauth_states_pending_bounds"),
}


@pytest.mark.parametrize("case", sorted(REJECTS))
def test_0007_checks_reject(db: Conn, case: str) -> None:
    extra, constraint = REJECTS[case]
    tenant, user = _world(db)
    columns: dict[str, Any] = {
        "tenant_id": tenant,
        "user_id": user,
        "state_hash": _digest(case),
        "marketplace": "amazon",
        "expires_at": "2099-01-01T00:00:00Z",
        **extra,
    }
    names = ", ".join(columns)
    marks = ", ".join(["%s"] * len(columns))
    expect_sqlstate(
        db,
        "23514",
        f"INSERT INTO oauth_states ({names}) VALUES ({marks})",  # noqa: S608
        list(columns.values()),
        constraint=constraint,
    )


def test_0007_accepts_state_row_and_pending_row(db: Conn) -> None:
    tenant, user = _world(db)
    _state(db, tenant, user, "plain-state", code_verifier="v" * 64)
    _state(db, tenant, user, "amazon-state")  # Amazon: no PKCE verifier
    _state(db, tenant, user, "pending", **PENDING)
    _state(db, tenant, user, "pending-min", **{**PENDING, "region": None, "scopes": None})
    _state(db, tenant, user, "seller-64", **{**PENDING, "seller_id": "A" * 64})
    _state(db, tenant, user, "token-16384", **{**PENDING, "token_enc": b"x" * 16384})


@pytest.mark.parametrize("role", ["app_ro", "app_platform_ro"])
def test_0007_readonly_roles_cannot_read_new_columns(db: Conn, role: str) -> None:
    for column in sorted(NEW_COLUMNS):
        row = db.execute(
            "SELECT has_column_privilege(%s, 'public.oauth_states', %s, 'SELECT')",
            [role, column],
        ).fetchone()
        assert row == (False,), column


def test_0007_app_rw_insert_delete_pending(db: Conn) -> None:
    tenant, user = _world(db)
    set_gucs(db, tenant_id=tenant)
    with as_app_rw(db):
        pending = _state(db, tenant, user, "rw-pending", **PENDING)
        deleted = db.execute(
            "DELETE FROM oauth_states WHERE id = %s AND token_enc IS NOT NULL RETURNING id",
            [pending],
        ).fetchall()
        assert deleted == [(pending,)]
