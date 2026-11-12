"""Revision 0038_d5 (client item 20): the brand setting `tenants.auto_fix_enabled` (on by default,
app_rw UPDATE, audited) and the held repair (`listing_repairs.held`: a suggested fix, released
once, never held again). Raw SQL, as in `test_0032_owner_self_approval.py`."""

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
from .integrity_support import as_app_rw, flow, scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0037_signup_info_request"
REVISION = "0038_d5"
PRIVILEGE = "SELECT has_column_privilege(%s, %s, %s, %s)"
RELEASE = "UPDATE listing_repairs SET held = false WHERE id = %s"
HOLD = "UPDATE listing_repairs SET held = true WHERE id = %s"


def test_chained_on_0037() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_the_setting_is_on_by_default(db: Conn) -> None:
    row = db.execute(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'tenants' AND column_name = 'auto_fix_enabled'"
    ).fetchone()
    assert row == ("boolean", "NO", "true")
    assert scalar(db, "SELECT auto_fix_enabled FROM tenants WHERE id = %s", (f.tenant(db),)) is True


def test_privileges(db: Conn) -> None:
    assert scalar(db, PRIVILEGE, ("app_rw", "public.tenants", "auto_fix_enabled", "UPDATE"))
    assert scalar(db, PRIVILEGE, ("app_rw", "public.listing_repairs", "held", "UPDATE"))
    assert not scalar(db, PRIVILEGE, ("app_rw", "public.listing_repairs", "rule", "UPDATE"))
    for role in ("app_ro", "app_platform_ro"):
        assert scalar(db, PRIVILEGE, (role, "public.tenants", "auto_fix_enabled", "SELECT"))
        assert scalar(db, PRIVILEGE, (role, "public.listing_repairs", "held", "SELECT"))


def test_audit_allowlist_keeps_the_setting(db: Conn) -> None:
    assert "auto_fix_enabled" in set(scalar(db, "SELECT audit_allowlist('tenant')"))


def test_a_repair_is_live_unless_held(db: Conn) -> None:
    fl = flow(db)
    live = f.listing_repair(db, fl.tenant_id, fl.listing_id)
    assert scalar(db, "SELECT held FROM listing_repairs WHERE id = %s", (live,)) is False


def test_a_held_repair_is_released_once_and_never_held_again(db: Conn) -> None:
    fl = flow(db)
    repair = f.listing_repair(db, fl.tenant_id, fl.listing_id, held=True)
    with as_app_rw(db, fl.tenant_id):
        db.execute(RELEASE, (repair,))
        assert scalar(db, "SELECT held FROM listing_repairs WHERE id = %s", (repair,)) is False
        expect_sqlstate(db, "GRD01", HOLD, (repair,))


def test_a_retired_repair_still_never_changes(db: Conn) -> None:
    fl = flow(db)
    repair = f.listing_repair(db, fl.tenant_id, fl.listing_id, held=True)
    db.execute(
        "UPDATE listing_repairs SET retired_at = now(), retired_reason = 'superseded' "
        "WHERE id = %s",
        (repair,),
    )
    with as_app_rw(db, fl.tenant_id):
        expect_sqlstate(db, "GRD01", RELEASE, (repair,))


def test_a_held_repair_takes_the_one_live_slot(db: Conn) -> None:
    fl = flow(db)
    f.listing_repair(db, fl.tenant_id, fl.listing_id, held=True)
    with pytest.raises(psycopg.errors.UniqueViolation) as caught, db.transaction():
        f.listing_repair(db, fl.tenant_id, fl.listing_id)
    assert caught.value.diag.constraint_name == "uq_listing_repairs_live"


def test_upgrade_downgrade_upgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    has = (
        "SELECT count(*) FROM information_schema.columns WHERE (table_name, column_name) IN "
        "(('tenants', 'auto_fix_enabled'), ('listing_repairs', 'held'))"
    )
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, has) == 0
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, has) == 2
        assert "auto_fix_enabled" in scalar(conn, "SELECT audit_allowlist('tenant')")
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, has) == 0
        assert "auto_fix_enabled" not in scalar(conn, "SELECT audit_allowlist('tenant')")
    command.upgrade(cfg, "head")
    command.check(cfg)
