"""Revision 0036_section_locks (client feedback 14): the single head, the round trip of the
listing locks and the run reason columns, and their CHECKs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, get_args

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.workers.payloads import RunReason

from .conftest import ScratchDb
from .integrity_support import scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0035_variant_photo_match"
REVISION = "0036_section_locks"
COLUMNS = (("listings", "locked_paths"), ("agent_runs", "run_reason"), ("agent_runs", "run_scope"))
CHECKS = ("ck_listings_locked_paths", "ck_agent_runs_run_reason", "ck_agent_runs_run_scope")


def _columns(conn: Conn) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND (table_name, column_name) IN "
        "(('listings', 'locked_paths'), ('agent_runs', 'run_reason'), ('agent_runs', 'run_scope'))"
    ).fetchall()
    return {(str(t), str(c)) for t, c in rows}


def _checks(conn: Conn) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY(%s)", (list(CHECKS),)
    ).fetchall()
    return {str(name): bool(valid) for name, valid in rows}


def test_single_head_on_0035() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    # 0037_signup_info_request sits on top (`test_0037_signup_info_request.py`)
    assert REVISION in {r.revision for r in script.walk_revisions()}
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_upgrade_downgrade_upgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _columns(conn) == set()
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _columns(conn) == set(COLUMNS)
        assert _checks(conn) == dict.fromkeys(CHECKS, True)  # validated
        default = scalar(
            conn,
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'listings' AND column_name = 'locked_paths'",
        )
        assert "'{}'" in str(default)
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _columns(conn) == set() and _checks(conn) == {}
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_run_reason_check_names_the_reasons(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    command.upgrade(alembic_env(None), "head")
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        definition = str(
            scalar(
                conn,
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
                ("ck_agent_runs_run_reason",),
            )
        )
    # the CHECK and the worker's `RunReason` name the same reasons
    for reason in get_args(RunReason):
        assert f"'{reason}'" in definition
    assert definition.count("'") == 2 * len(get_args(RunReason))
