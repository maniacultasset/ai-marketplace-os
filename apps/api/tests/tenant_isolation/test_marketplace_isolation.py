"""S15 T18 (DW2, AC4): marketplace accounts and OAuth state rows under FORCE RLS, raw SQL as the
`app_rw` login. Another tenant's state, pending and account rows are invisible; the callback's
hash branch (no tenant GUC, `app.token_hash` only) can read and delete a live *state* row by its
hash and never sees, deletes or updates a *pending* row; the one-live-seller key holds across
tenants beneath RLS."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
import pytest
import tests.integration.ops_support as ops

Conn = psycopg.Connection[tuple[Any, ...]]


def _digest(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


@contextmanager
def session(conn: Conn, tenant: UUID | None = None, **gucs: str) -> Iterator[Conn]:
    """One rolled-back app_rw transaction with `app.tenant_id` and any other `app.*` GUC set."""
    with conn.transaction(force_rollback=True):
        if tenant is not None:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant)])
        for name, value in gucs.items():
            conn.execute("SELECT set_config(%s, %s, true)", [f"app.{name}", value])
        yield conn


@pytest.fixture
def sides(admin_conn: Conn, clean_tenants: None) -> tuple[Any, Any]:
    return ops.seed_tenant(admin_conn), ops.seed_tenant(admin_conn)


def _state(admin: Conn, side: Any, label: str) -> UUID:
    return ops.oauth_state(admin, side, state_hash=_digest(label))


def _pending(admin: Conn, side: Any, label: str) -> UUID:
    return ops.oauth_state(
        admin,
        side,
        state_hash=_digest(label),
        token_enc=b"MKT1" + b"\x00" * 60,
        kms_key_version="local-1",
        seller_id="A1PENDING",
    )


def _ids(conn: Conn, query: str, params: list[Any] | None = None) -> set[Any]:
    return {row[0] for row in conn.execute(query, params).fetchall()}


def test_iso_oauth_states_other_tenant_invisible(
    admin_conn: Conn, app_conn: Conn, sides: tuple[Any, Any]
) -> None:
    a, b = sides
    mine = _state(admin_conn, a, "a-state")
    theirs = {_state(admin_conn, b, "b-state"), _pending(admin_conn, b, "b-pending")}
    with session(app_conn, a.tenant_id):
        visible = _ids(app_conn, "SELECT id FROM oauth_states")
        assert theirs.isdisjoint(visible) and mine in visible
        deleted = app_conn.execute(
            "DELETE FROM oauth_states WHERE id = ANY(%s)", [list(theirs)]
        ).rowcount
        assert deleted == 0
    assert _ids(admin_conn, "SELECT id FROM oauth_states") >= theirs


def test_iso_oauth_state_hash_branch_delete_state_row(
    admin_conn: Conn, app_conn: Conn, sides: tuple[Any, Any]
) -> None:
    _, b = sides
    row = _state(admin_conn, b, "b-callback")
    with session(app_conn, token_hash=_digest("b-callback").hex()):
        deleted = app_conn.execute(
            "DELETE FROM oauth_states WHERE state_hash = %s RETURNING id, tenant_id",
            [_digest("b-callback")],
        ).fetchall()
    assert deleted == [(row, b.tenant_id)]


def test_iso_oauth_state_hash_branch_hides_pending_row(
    admin_conn: Conn, app_conn: Conn, sides: tuple[Any, Any]
) -> None:
    _, b = sides
    row = _pending(admin_conn, b, "b-handle")
    with session(app_conn, token_hash=_digest("b-handle").hex()):
        assert _ids(app_conn, "SELECT id FROM oauth_states") == set()
        assert (
            app_conn.execute(
                "DELETE FROM oauth_states WHERE state_hash = %s", [_digest("b-handle")]
            ).rowcount
            == 0
        )
    assert _ids(admin_conn, "SELECT id FROM oauth_states WHERE id = %s", [row]) == {row}


def test_iso_oauth_state_hash_branch_cannot_update(
    admin_conn: Conn, app_conn: Conn, sides: tuple[Any, Any]
) -> None:
    _, b = sides
    _state(admin_conn, b, "b-update")
    _pending(admin_conn, b, "b-update-pending")
    for label in ("b-update", "b-update-pending"):
        with session(app_conn, token_hash=_digest(label).hex()):
            updated = app_conn.execute(
                "UPDATE oauth_states SET expires_at = now() + interval '1 day' "
                "WHERE state_hash = %s",
                [_digest(label)],
            ).rowcount
            assert updated == 0


def test_iso_oauth_state_hash_branch_other_hash_hidden(
    admin_conn: Conn, app_conn: Conn, sides: tuple[Any, Any]
) -> None:
    a, b = sides
    _state(admin_conn, a, "a-other")
    target = _state(admin_conn, b, "b-other")
    with session(app_conn, token_hash=_digest("b-other").hex()):
        assert _ids(app_conn, "SELECT id FROM oauth_states") == {target}
        assert (
            app_conn.execute(
                "DELETE FROM oauth_states WHERE state_hash = %s", [_digest("a-other")]
            ).rowcount
            == 0
        )


def test_iso_marketplace_accounts_other_tenant_invisible(
    app_conn: Conn, sides: tuple[Any, Any]
) -> None:
    a, b = sides
    with session(app_conn, a.tenant_id):
        assert _ids(app_conn, "SELECT id FROM marketplace_accounts") == {a.account_id}
        assert (
            app_conn.execute(
                "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
                "kms_key_version = NULL WHERE id = %s",
                [b.account_id],
            ).rowcount
            == 0
        )


def test_iso_account_insert_other_tenant_refused(app_conn: Conn, sides: tuple[Any, Any]) -> None:
    a, b = sides
    with session(app_conn, a.tenant_id), pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(
            "INSERT INTO marketplace_accounts (tenant_id, marketplace, seller_id, token_enc, "
            "kms_key_version, connected_by_user_id) VALUES (%s, 'flipkart', 'F1X', %s, "
            "'local-1', %s)",
            [b.tenant_id, b"MKT1" + b"\x00" * 60, b.user_id],
        )


def test_iso_seller_live_in_acme_refused_in_bharat(
    admin_conn: Conn, app_conn: Conn, sides: tuple[Any, Any]
) -> None:
    from app.db.models.catalog_assets import UQ_MA_SELLER_LIVE

    a, b = sides
    seller = admin_conn.execute(
        "SELECT marketplace, seller_id FROM marketplace_accounts WHERE id = %s", [a.account_id]
    ).fetchone()
    assert seller is not None
    admin_conn.execute(
        "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
        "kms_key_version = NULL WHERE id = %s",
        [b.account_id],
    )
    with session(app_conn, b.tenant_id), pytest.raises(psycopg.errors.UniqueViolation) as info:
        app_conn.execute(
            "INSERT INTO marketplace_accounts (tenant_id, marketplace, seller_id, token_enc, "
            "kms_key_version, connected_by_user_id) VALUES (%s, %s, %s, %s, 'local-1', %s)",
            [b.tenant_id, seller[0], seller[1], b"MKT1" + b"\x00" * 60, b.user_id],
        )
    assert info.value.diag.constraint_name == UQ_MA_SELLER_LIVE
    # bharat still cannot see acme's row: the index answers beneath RLS, the read does not
    with session(app_conn, b.tenant_id):
        assert a.account_id not in _ids(app_conn, "SELECT id FROM marketplace_accounts")


def test_iso_audit_allowlist_covers_marketplace_account(admin_conn: Conn) -> None:
    row = admin_conn.execute("SELECT public.audit_allowlist('marketplace_account')").fetchone()
    assert row is not None and "status" in row[0]
