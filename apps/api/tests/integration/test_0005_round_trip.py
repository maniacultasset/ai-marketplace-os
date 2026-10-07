"""The 0005_rls round trip on a scratch database through env.py (DATABASE.md §8.2, §8.3): up to
head, down to 0004_integrity leaves no policy and no table with RLS enabled or forced, and the
same tables, grants and functions as 0004 (`listing_issues_guard` back to its exact 0004 body);
the next upgrade rebuilds the same policies and the fail-closed guard; the full cycle passes
`alembic check` (run at the head: later revisions sit above 0005, as test_0004_round_trip does)."""

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


def _issues_guard(db: ScratchDb) -> str:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        row = conn.execute(
            "SELECT pg_get_functiondef('public.listing_issues_guard()'::regprocedure)"
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_0005_round_trip(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    at_previous, grants_previous = _objects(scratch_db), _grants(scratch_db)
    assert _rls(scratch_db) == ([], [])
    guard_previous = _issues_guard(scratch_db)
    assert "AND EXISTS (SELECT 1 FROM public.rules r" in guard_previous
    command.upgrade(cfg, REVISION)
    at_head = _rls(scratch_db)
    guard_head = _issues_guard(scratch_db)
    assert "AND NOT EXISTS (SELECT 1 FROM public.rules r" in guard_head
    flags, policies = at_head
    assert len(flags) > 30 and len(policies) > 3 * 30
    command.downgrade(cfg, PREVIOUS)
    assert _rls(scratch_db) == ([], [])
    assert _objects(scratch_db) == at_previous
    assert _grants(scratch_db) == grants_previous
    assert _issues_guard(scratch_db) == guard_previous
    command.upgrade(cfg, REVISION)
    assert _rls(scratch_db) == at_head
    assert _issues_guard(scratch_db) == guard_head
    command.upgrade(cfg, "head")
    command.check(cfg)
