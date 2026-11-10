"""Revision 0028_photo_read_on_upload (V1.1): the single head, the round trip of the widened job
entity list (from the parent's list, read live) and the guarded downgrade."""

from __future__ import annotations

import re
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

PREVIOUS = "0027_v1_1_assist"
REVISION = "0028_photo_read_on_upload"
ENTITY_CHECK = "ck_job_runs_entity_type"


def _members(conn: Conn) -> set[str]:
    definition = scalar(
        conn,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
        (ENTITY_CHECK,),
    )
    return set(re.findall(r"'([a-z_]+)'", str(definition)))


def test_single_head_on_0027() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == [REVISION]
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_upgrade_downgrade_upgrade_widens_the_entity_list(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        parent = _members(conn)
    assert "column_map" in parent and "photo_facts_read" not in parent
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _members(conn) == parent | {"photo_facts_read"}
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _members(conn) == parent
    command.upgrade(cfg, "head")
    command.check(cfg)
