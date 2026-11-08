"""Revision 0021_ai_credit_state (SI10 T2, AC2): the round trip, the one-row table and its CHECKs,
the app_rw grants under forced RLS, the clear guard (only staff clear a pause) and the CP-D3
auto-clear function (a credit pause clears only when a limited key's remaining credit rose to the
floor or above)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import as_app_rw, scalar, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0018_image_generation"
REVISION = "0021_ai_credit_state"
PAUSE = (
    "UPDATE ai_credit_state SET paused_reason = %s, paused_at = now() "
    "WHERE id = 1 AND paused_at IS NULL RETURNING id"
)
RECORD = (
    "UPDATE ai_credit_state SET limit_usd = 10, usage_usd = 9.6, remaining_usd = 0.4, "
    "checked_at = now() WHERE id = 1"
)
RESUME = (
    "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL, resumed_at = now(), "
    "resumed_by_user_id = app_user_id() WHERE id = 1"
)
AUTO_CLEAR = "SELECT app_ai_credit_auto_clear(%s::numeric)"


def _exists(db: ScratchDb) -> bool:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        return bool(scalar(conn, "SELECT to_regclass('public.ai_credit_state') IS NOT NULL"))


def _row(conn: Conn) -> tuple[Any, ...]:
    found = conn.execute(
        "SELECT paused_reason, paused_at, remaining_usd, resumed_at, resumed_by_user_id "
        "FROM ai_credit_state WHERE id = 1"
    ).fetchone()
    assert found is not None
    return tuple(found)


def _seed(conn: Conn, *, reason: str | None, remaining: str | None) -> None:
    """Row 1 as the superuser (the `db` login): past every guard."""
    conn.execute(
        "UPDATE ai_credit_state SET paused_reason = %s, "
        "paused_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END, "
        "remaining_usd = %s::numeric, resumed_at = NULL, resumed_by_user_id = NULL WHERE id = 1",
        (reason, reason, remaining),
    )


def _admin(conn: Conn) -> UUID:
    return f.user(conn, is_platform_admin=True)


# ---- round trip ----


def test_upgrade_downgrade_upgrade_clean(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    assert not _exists(scratch_db)
    command.upgrade(cfg, REVISION)
    assert _exists(scratch_db)
    command.upgrade(cfg, "head")  # `check` compares the code's head
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert not _exists(scratch_db)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM pg_proc WHERE proname IN "
                "('app_ai_credit_auto_clear', 'ai_credit_state_guard')",
            )
            == 0
        )
    command.upgrade(cfg, "head")
    command.check(cfg)


# ---- the one row and its CHECKs ----


def test_row_one_is_seeded_unpaused(db: Conn) -> None:
    _seed(db, reason=None, remaining=None)
    assert scalar(db, "SELECT count(*) FROM ai_credit_state") == 1
    assert _row(db)[:2] == (None, None)


def test_second_row_refused_by_check(db: Conn) -> None:
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO ai_credit_state (id) VALUES (2)",
        constraint="ck_ai_credit_state_one_row",
    )


def test_app_rw_cannot_insert(db: Conn) -> None:
    with as_app_rw(db):
        expect_sqlstate(db, "42501", "INSERT INTO ai_credit_state (id) VALUES (2)")


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (
            "UPDATE ai_credit_state SET paused_reason = 'key_invalid' WHERE id = 1",
            "ck_ai_credit_state_paused",
        ),
        ("UPDATE ai_credit_state SET paused_at = now() WHERE id = 1", "ck_ai_credit_state_paused"),
        (
            "UPDATE ai_credit_state SET paused_reason = 'tired', paused_at = now() WHERE id = 1",
            "ck_ai_credit_state_reason",
        ),
    ],
)
def test_pause_pair_and_reason_checked(db: Conn, statement: str, constraint: str) -> None:
    _seed(db, reason=None, remaining=None)
    expect_sqlstate(db, "23514", statement, constraint=constraint)


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM ai_credit_state",
        "TRUNCATE ai_credit_state",
        "UPDATE ai_credit_state SET id = 1 WHERE id = 1",
    ],
)
def test_app_rw_cannot_delete_truncate_or_move_id(db: Conn, statement: str) -> None:
    with as_app_rw(db):
        expect_sqlstate(db, "42501", statement)


@pytest.mark.parametrize("role", ["app_ro", "app_platform_ro", "app_rw"])
def test_roles_can_read(db: Conn, role: str) -> None:
    db.execute(f"SET ROLE {role}")
    try:
        assert scalar(db, "SELECT count(*) FROM ai_credit_state") == 1
    finally:
        db.execute("RESET ROLE")


def test_rls_enabled_and_forced(db: Conn) -> None:
    found = db.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE oid = 'public.ai_credit_state'::regclass"
    ).fetchone()
    assert found == (True, True)


# ---- the guard (AC2) ----


def test_app_rw_records_and_pauses_but_cannot_clear(db: Conn) -> None:
    _seed(db, reason=None, remaining=None)
    tenant = f.tenant(db)
    with as_app_rw(db, tenant):
        db.execute(RECORD)
        assert db.execute(PAUSE, ("credit_exhausted",)).fetchone() is not None
        expect_sqlstate(
            db, "GRD01", "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL"
        )
        expect_sqlstate(db, "GRD01", "UPDATE ai_credit_state SET paused_reason = 'key_invalid'")
        expect_sqlstate(db, "GRD01", "UPDATE ai_credit_state SET resumed_at = now()")
    assert _row(db)[0] == "credit_exhausted"
    assert _row(db)[2] == Decimal("0.4")


def test_non_admin_user_cannot_resume(db: Conn) -> None:
    _seed(db, reason="key_invalid", remaining=None)
    user = f.user(db)
    set_gucs(db, user_id=user)
    with as_app_rw(db):
        expect_sqlstate(db, "GRD01", RESUME)
    assert _row(db)[0] == "key_invalid"


def test_non_admin_cannot_set_resumed_on_unpaused_row(db: Conn) -> None:
    _seed(db, reason=None, remaining=None)
    set_gucs(db, user_id=f.user(db))
    with as_app_rw(db):
        expect_sqlstate(db, "GRD01", RESUME)


def test_platform_admin_resumes(db: Conn) -> None:
    _seed(db, reason="credit_exhausted", remaining="0.4")
    admin = _admin(db)
    set_gucs(db, user_id=admin)
    with as_app_rw(db):
        db.execute(RESUME)
        db.execute(RESUME)  # repeatable
    reason, paused_at, _, resumed_at, resumed_by = _row(db)
    assert (reason, paused_at) == (None, None)
    assert resumed_at is not None
    assert resumed_by == admin


def test_admin_cannot_record_another_user_as_resumer(db: Conn) -> None:
    _seed(db, reason="credit_exhausted", remaining="0.4")
    other = f.user(db)
    set_gucs(db, user_id=_admin(db))
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "GRD01",
            "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL, "
            "resumed_at = now(), resumed_by_user_id = %s WHERE id = 1",
            (other,),
        )


def test_inactive_admin_cannot_resume(db: Conn) -> None:
    _seed(db, reason="credit_exhausted", remaining="0.4")
    set_gucs(db, user_id=f.user(db, is_platform_admin=True, is_active=False))
    with as_app_rw(db):
        expect_sqlstate(db, "GRD01", RESUME)


def test_pause_first_wins(db: Conn) -> None:
    _seed(db, reason=None, remaining=None)
    with as_app_rw(db):
        assert db.execute(PAUSE, ("credit_exhausted",)).fetchone() is not None
        first = _row(db)[1]
        assert db.execute(PAUSE, ("key_invalid",)).fetchone() is None
    assert _row(db)[:2] == ("credit_exhausted", first)


def test_resumer_user_deleted_sets_null(db: Conn) -> None:
    _seed(db, reason="credit_exhausted", remaining="0.4")
    admin = _admin(db)
    set_gucs(db, user_id=admin)
    with as_app_rw(db):
        db.execute(RESUME)
    db.execute("DELETE FROM users WHERE id = %s", (admin,))
    assert _row(db)[4] is None


# ---- the auto-clear function (AC2, CP-D3) ----


@pytest.mark.parametrize(
    ("stored", "new", "cleared"),
    [
        ("0.40", "0.60", True),
        ("0.40", "0.50", True),  # at the floor (ai_credit.CREDIT_FLOOR_USD)
        ("0.40", "0.499999", False),
        ("5.00", "5.00", False),  # flat: an account-level 402 with a healthy key limit
        ("0.40", "0.45", False),  # rose, below the floor
        (None, "0.60", True),
        ("0.40", None, False),  # an unlimited key never auto-clears a credit pause
        (None, None, False),
    ],
)
def test_auto_clear_credit_exhausted(
    db: Conn, stored: str | None, new: str | None, cleared: bool
) -> None:
    _seed(db, reason="credit_exhausted", remaining=stored)
    with as_app_rw(db):
        assert scalar(db, AUTO_CLEAR, (new,)) is cleared
    assert _row(db)[0] == (None if cleared else "credit_exhausted")
    assert _row(db)[2] == (None if stored is None else Decimal(stored))  # never records


@pytest.mark.parametrize("new", ["0.00", "9.00", None])
def test_auto_clear_key_invalid_on_any_value(db: Conn, new: str | None) -> None:
    _seed(db, reason="key_invalid", remaining="0.10")
    with as_app_rw(db):
        assert scalar(db, AUTO_CLEAR, (new,)) is True
    assert _row(db)[:2] == (None, None)


def test_auto_clear_unpaused_row_is_a_no_op(db: Conn) -> None:
    _seed(db, reason=None, remaining="3.00")
    before = scalar(db, "SELECT updated_at FROM ai_credit_state WHERE id = 1")
    with as_app_rw(db):
        assert scalar(db, AUTO_CLEAR, ("9.00",)) is False
    assert scalar(db, "SELECT updated_at FROM ai_credit_state WHERE id = 1") == before


@pytest.mark.parametrize("role", ["app_ro", "app_platform_ro"])
def test_auto_clear_is_app_rw_only(db: Conn, role: str) -> None:
    db.execute(f"SET ROLE {role}")
    try:
        expect_sqlstate(db, "42501", AUTO_CLEAR, ("1.00",))
    finally:
        db.execute("RESET ROLE")


def test_auto_clear_owner_is_migrator(db: Conn) -> None:
    owner = scalar(
        db,
        "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname = 'app_ai_credit_auto_clear'",
    )
    assert owner == "migrator"
