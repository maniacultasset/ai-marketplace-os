"""Revision 0029_admin_google_link (ADR-0020 amendment, 11 Nov 2026): the single head, the link
definer that now links a platform admin (0012's refused), and the guarded downgrade."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from . import factories as f
from .conftest import ScratchDb
from .integrity_support import as_app_rw, scalar, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0028_photo_read_on_upload"
REVISION = "0029_admin_google_link"
DEFINER = "public.app_link_google_sub(uuid,text)"


def _link(conn: Conn, user_id: UUID, sub: str) -> bool:
    set_gucs(conn, user_id=user_id)
    with as_app_rw(conn):
        return bool(scalar(conn, "SELECT app_link_google_sub(%s, %s)", (user_id, sub)))


def _admin_links(libpq: str, sub: str) -> bool:
    """A new platform admin tries to link `sub`; the attempt is rolled back."""
    with psycopg.connect(libpq) as conn:
        try:
            return _link(conn, f.user(conn, is_platform_admin=True), sub)
        finally:
            conn.rollback()


def _definer(libpq: str) -> tuple[Any, ...]:
    with psycopg.connect(libpq, autocommit=True) as conn:
        row = conn.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig, "
            "has_function_privilege('app_rw', p.oid, 'EXECUTE'), "
            "has_function_privilege('public', p.oid, 'EXECUTE') "
            "FROM pg_proc p WHERE p.oid = %s::regprocedure",
            (DEFINER,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def test_chained_on_0028() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    # 0030_candidate_cache_accepted sits on top (`test_0030_candidate_cache_accepted.py`)
    assert REVISION in {r.revision for r in script.walk_revisions()}
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_upgrade_downgrade_upgrade_links_an_admin_only_at_0029(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    before = _definer(scratch_db.libpq)
    assert _admin_links(scratch_db.libpq, "sub-a1") is False  # 0012's refusal
    command.upgrade(cfg, REVISION)
    assert _admin_links(scratch_db.libpq, "sub-a2") is True
    assert _definer(scratch_db.libpq) == before  # owner, search_path and grants kept
    command.downgrade(cfg, PREVIOUS)
    assert _admin_links(scratch_db.libpq, "sub-a3") is False
    assert _definer(scratch_db.libpq) == before
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_link_keeps_every_other_condition(db: Conn) -> None:
    admin = f.user(db, is_platform_admin=True)
    other = f.user(db, google_sub="sub-taken")
    assert _link(db, admin, "sub-taken") is False  # someone else's Google account
    assert _link(db, admin, "sub-b1") is True
    assert _link(db, admin, "sub-b2") is False  # already linked: never replaced
    assert scalar(db, "SELECT google_sub FROM users WHERE id = %s", (other,)) == "sub-taken"
    no_password = f.user(db, is_platform_admin=True, hashed_password=None, auth_provider="google")
    assert _link(db, no_password, "sub-b3") is False
    inactive = f.user(db, is_platform_admin=True, is_active=False)
    assert _link(db, inactive, "sub-b4") is False
    someone = f.user(db, is_platform_admin=True)
    set_gucs(db, user_id=other)
    with as_app_rw(db):  # only for the caller's own row
        assert scalar(db, "SELECT app_link_google_sub(%s, 'sub-b5')", (someone,)) is False


def test_downgrade_refused_while_an_admin_is_linked(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        admin = f.user(conn, is_platform_admin=True, google_sub="sub-c1")
    with pytest.raises(Exception, match="0029 downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        conn.execute("UPDATE users SET google_sub = NULL WHERE id = %s", (admin,))
    command.downgrade(cfg, PREVIOUS)
    command.upgrade(cfg, "head")
