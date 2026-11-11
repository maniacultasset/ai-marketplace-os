"""Revision 0031_signup_status_handle (ADR-0019 amendment): the single head, the round trip of
the two handle columns, their CHECK, the hash index, the read policy and the definer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import as_app_rw, scalar, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0030_candidate_cache_accepted"
REVISION = "0031_signup_status_handle"


def _shape(conn: Conn) -> tuple[Any, ...]:
    columns = scalar(
        conn,
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'signup_requests' "
        "AND column_name IN ('status_handle_hash', 'status_handle_expires_at')",
    )
    check = scalar(
        conn,
        "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_signup_requests_status_handle' "
        "AND convalidated",
    )
    index = scalar(
        conn,
        "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = 'uq_signup_requests_status_handle_hash' AND i.indisunique "
        "AND i.indisvalid",
    )
    policy = scalar(
        conn,
        "SELECT count(*) FROM pg_policies WHERE tablename = 'signup_requests' "
        "AND policyname = 'sr_handle_select'",
    )
    definer = scalar(
        conn,
        "SELECT count(*) FROM pg_proc WHERE proname = 'app_set_signup_status_handle' AND prosecdef",
    )
    guard = scalar(
        conn,
        "SELECT count(*) FROM pg_trigger WHERE tgname = 'signup_requests_status_handle_guard'",
    )
    return (columns, check, index, policy, definer, guard)


def test_single_head_on_0030() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == [REVISION]
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_upgrade_downgrade_upgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _shape(conn) == (0, 0, 0, 0, 0, 0)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _shape(conn) == (2, 1, 1, 1, 1, 1)
        # the read-only roles never read the hash; app_rw cannot write it
        for role in ("app_ro", "app_platform_ro"):
            assert (
                scalar(
                    conn,
                    "SELECT has_column_privilege(%s, 'public.signup_requests', "
                    "'status_handle_hash', 'SELECT')",
                    (role,),
                )
                is False
            )
        assert (
            scalar(
                conn,
                "SELECT has_column_privilege('app_rw', 'public.signup_requests', "
                "'status_handle_hash', 'UPDATE')",
            )
            is False
        )
        assert (
            scalar(
                conn,
                "SELECT has_function_privilege('app_rw', "
                "'public.app_set_signup_status_handle(uuid, bytea)', 'EXECUTE')",
            )
            is True
        )
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _shape(conn) == (0, 0, 0, 0, 0, 0)
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_check_refuses_a_half_set_or_short_handle(db: Conn) -> None:
    user = f.user(db)
    request = f.insert(db, "signup_requests", user_id=user, brand_name="B", owner_name="O")
    for sets in (
        "status_handle_hash = decode(repeat('ab', 32), 'hex')",
        "status_handle_expires_at = now()",
        "status_handle_hash = decode('ab', 'hex'), status_handle_expires_at = now()",
    ):
        expect_sqlstate(
            db,
            "23514",
            f"UPDATE signup_requests SET {sets} WHERE id = %s",  # noqa: S608 - fixed literals
            (request,),
        )


def test_app_rw_insert_with_a_handle_grd01(db: Conn) -> None:
    """app_rw keeps table-level INSERT; the guard keeps the handle columns the definer's alone."""
    user_id = f.user(db)
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        for columns, values in (
            (
                "status_handle_hash, status_handle_expires_at",
                "decode(repeat('ab', 32), 'hex'), 'infinity'",
            ),
            ("status_handle_hash", "decode(repeat('ab', 32), 'hex')"),
            ("status_handle_expires_at", "'infinity'"),
        ):
            expect_sqlstate(
                db,
                "GRD01",
                "INSERT INTO signup_requests (user_id, brand_name, owner_name, "  # noqa: S608
                f"{columns}) VALUES (%s, 'B', 'O', {values})",  # fixed literals
                (user_id,),
            )
        db.execute(
            "INSERT INTO signup_requests (user_id, brand_name, owner_name) VALUES (%s, 'B', 'O')",
            (user_id,),
        )
