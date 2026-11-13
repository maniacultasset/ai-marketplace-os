"""Revision 0039_product_archive (Chrome pass 2): `products.archived_at`, a nullable column that
app_rw writes and the read roles read through the products table grants."""

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

PREVIOUS = "0038_d5"
REVISION = "0039_product_archive"
PRIVILEGE = "SELECT has_column_privilege(%s, %s, %s, %s)"
HAS = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'products' AND column_name = 'archived_at'"
)


def test_chained_on_0038() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_the_column_is_nullable_with_no_default(db: Conn) -> None:
    row = db.execute(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'products' AND column_name = 'archived_at'"
    ).fetchone()
    assert row == ("timestamp with time zone", "YES", None)


def test_privileges(db: Conn) -> None:
    assert scalar(db, PRIVILEGE, ("app_rw", "public.products", "archived_at", "UPDATE"))
    for role in ("app_ro", "app_platform_ro"):
        assert scalar(db, PRIVILEGE, (role, "public.products", "archived_at", "SELECT"))


def test_upgrade_downgrade_upgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, HAS) == 0
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, HAS) == 1
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, HAS) == 0
    command.upgrade(cfg, "head")
    command.check(cfg)
