"""Revision 0041_category_suggestion_flags (client feedback 17 Nov): the single head, the stored
suggestion's two optional flags behind `category_suggestion_valid()`, and the round trip."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from .conftest import ScratchDb
from .integrity_support import scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0040_sheet_photo_links"
REVISION = "0041_category_suggestion_flags"
VALID = "SELECT category_suggestion_valid(%s::jsonb)"
BASE = '"status": "suggested", "candidates": [], "at": "2026-11-17T10:00:00+00:00"'


def _valid(conn: Conn, extra: str = "") -> bool:
    return bool(scalar(conn, VALID, ("{" + BASE + extra + "}",)))


def test_chained_on_0040() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_flags_are_checked(db: Conn) -> None:
    assert _valid(db)
    assert _valid(db, ', "not_listed": true, "looks_like": "bottomwear"')
    assert _valid(db, ', "not_listed": false, "looks_like": null')
    assert not _valid(db, ', "not_listed": "yes"')
    assert not _valid(db, ', "looks_like": "dresses"')
    assert not _valid(db, ', "looks_like": 3')
    assert not _valid(db, ', "other_flag": true')


def test_upgrade_downgrade_upgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _valid(conn) and not _valid(conn, ', "not_listed": true')
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _valid(conn, ', "not_listed": true, "looks_like": "topwear"')
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _valid(conn) and not _valid(conn, ', "looks_like": "topwear"')
    command.upgrade(cfg, "head")
    command.check(cfg)
