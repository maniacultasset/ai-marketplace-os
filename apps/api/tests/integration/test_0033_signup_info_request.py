"""Revision 0033_signup_info_request (client item 2, ADR-0019 amendment): the single head, the
round trip of the four info columns, their CHECK, the guard and the reply definer. The RLS half
(who may ask, who may reply) is in tests/tenant_isolation/test_signup_isolation.py."""

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
from .integrity_support import scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0032_owner_self_approval"
REVISION = "0033_signup_info_request"


def _shape(conn: Conn) -> tuple[Any, ...]:
    columns = scalar(
        conn,
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'signup_requests' "
        "AND column_name IN ('info_question', 'info_asked_at', 'info_reply', 'info_replied_at')",
    )
    check = scalar(
        conn,
        "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_signup_requests_info' "
        "AND convalidated",
    )
    definer = scalar(
        conn,
        "SELECT count(*) FROM pg_proc WHERE proname = 'app_reply_signup_info' AND prosecdef",
    )
    guard = scalar(
        conn, "SELECT count(*) FROM pg_trigger WHERE tgname = 'signup_requests_info_guard'"
    )
    return (columns, check, definer, guard)


def test_single_head_on_0032() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == [REVISION]
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_upgrade_downgrade_upgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _shape(conn) == (0, 0, 0, 0)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _shape(conn) == (4, 1, 1, 1)
        for column, granted in (
            ("info_question", True),
            ("info_asked_at", True),
            ("info_reply", False),
            ("info_replied_at", False),
        ):
            assert (
                scalar(
                    conn,
                    "SELECT has_column_privilege('app_rw', 'public.signup_requests', %s, "
                    "'UPDATE')",
                    (column,),
                )
                is granted
            ), column
        assert (
            scalar(
                conn,
                "SELECT has_function_privilege('app_rw', "
                "'public.app_reply_signup_info(bytea, text)', 'EXECUTE')",
            )
            is True
        )
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _shape(conn) == (0, 0, 0, 0)
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_check_refuses_a_half_set_or_orphan_reply(db: Conn) -> None:
    user = f.user(db)
    request = f.insert(db, "signup_requests", user_id=user, brand_name="B", owner_name="O")
    for sets in (
        "info_question = 'q'",
        "info_asked_at = now()",
        "info_reply = 'r', info_replied_at = now()",
        "info_question = '', info_asked_at = now()",
        "info_question = repeat('q', 1001), info_asked_at = now()",
        "info_question = 'q', info_asked_at = now(), info_reply = repeat('r', 1001), "
        "info_replied_at = now()",
    ):
        expect_sqlstate(
            db,
            "23514",
            f"UPDATE signup_requests SET {sets} WHERE id = %s",  # noqa: S608 - fixed literals
            (request,),
        )
    db.execute(
        "UPDATE signup_requests SET info_question = 'q', info_asked_at = now(), "
        "info_reply = 'r', info_replied_at = now() WHERE id = %s",
        (request,),
    )
