"""Revision 0030_sku_counters (client feedback 11 Nov): the single head, the round trip of the
counter table and the purge order, and the guarded downgrade."""

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
from .conftest import ScratchDb
from .integrity_support import scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0029_admin_google_link"
REVISION = "0030_sku_counters"


def _has_table(libpq: str) -> bool:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return scalar(conn, "SELECT to_regclass('public.sku_counters')") is not None


def test_single_head_on_0029() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == [REVISION]
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_upgrade_downgrade_upgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    assert not _has_table(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        purge_before = list(scalar(conn, "SELECT purge_tenant_order()"))
    command.upgrade(cfg, REVISION)
    assert _has_table(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        purge_after = list(scalar(conn, "SELECT purge_tenant_order()"))
        assert [t for t in purge_after if t != "sku_counters"] == purge_before
        assert purge_after.index("sku_counters") > purge_after.index("products")
        forced = scalar(
            conn, "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'sku_counters'"
        )
        assert forced is True
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert not _has_table(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert list(scalar(conn, "SELECT purge_tenant_order()")) == purge_before
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_downgrade_refused_while_a_counter_exists(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        tenant = f.tenant(conn)
        conn.execute(
            "INSERT INTO sku_counters (tenant_id, code, last_value) VALUES (%s, 'ACME-KUR', 1)",
            [tenant],
        )
        conn.commit()
    with pytest.raises(Exception, match="0030 downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT version_num FROM alembic_version") == REVISION


def test_code_and_value_checks(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        tenant = f.tenant(conn)
        for code, value in (("acme-kur", 1), ("ACME", 1), ("ACME-KUR", 0)):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO sku_counters (tenant_id, code, last_value) VALUES (%s, %s, %s)",
                    [tenant, code, value],
                )
