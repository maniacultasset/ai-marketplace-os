"""The 0005_rls round trip on a scratch database through env.py (DATABASE.md §8.2, §8.3): up to
head, down to 0004_integrity leaves no policy and no table with RLS enabled or forced, and the
same tables, grants and functions as 0004; the next upgrade rebuilds the same policies; the full
cycle passes `alembic check`."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from .conftest import ScratchDb
from .test_0002_catalog import _objects
from .test_0003_round_trip import _grants

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0004_integrity"
REVISION = "0005_rls"


def _rls(db: ScratchDb) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        flags = conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND (relrowsecurity OR relforcerowsecurity) ORDER BY 1"
        ).fetchall()
        policies = conn.execute(
            "SELECT tablename, policyname, permissive, cmd, roles::text, qual, with_check "
            "FROM pg_policies WHERE schemaname = 'public' ORDER BY 1, 2"
        ).fetchall()
    return flags, policies


def test_0005_round_trip(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    at_previous, grants_previous = _objects(scratch_db), _grants(scratch_db)
    assert _rls(scratch_db) == ([], [])
    command.upgrade(cfg, REVISION)
    command.check(cfg)
    at_head = _rls(scratch_db)
    flags, policies = at_head
    assert len(flags) > 30 and len(policies) > 3 * 30
    command.downgrade(cfg, PREVIOUS)
    assert _rls(scratch_db) == ([], [])
    assert _objects(scratch_db) == at_previous
    assert _grants(scratch_db) == grants_previous
    command.upgrade(cfg, REVISION)
    command.check(cfg)
    assert _rls(scratch_db) == at_head
