"""Revision 0019_gender_baby (SI8 T8; AC11): the round trip, `baby` on a style, `kids` still
refused, a variant still style-owned, and the downgrade that refuses while a `baby` row exists.
Run in the Docker batch (`make test-db`, `make api-tests`)."""

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
from .conftest import ScratchDb, sync_dictionary_on

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0020_status_truth"
REVISION = "0019_gender_baby"


def test_previous_is_the_down_revision() -> None:
    """The round trip isolates 0019 only when PREVIOUS is its direct parent."""
    from app.db.migrations import alembic_config

    revision = ScriptDirectory.from_config(alembic_config()).get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS


def _check(db: ScratchDb) -> str:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
            "WHERE conname = 'ck_products_gender'"
        ).fetchone()
    assert row is not None
    assert row[1] is True  # validated, never left NOT VALID
    return str(row[0])


def test_upgrade_downgrade_upgrade_clean(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    assert "baby" not in _check(scratch_db)
    command.upgrade(cfg, REVISION)
    assert "baby" in _check(scratch_db)
    command.upgrade(cfg, "head")  # 0021 sits on top (SI10): `check` compares at the head
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert "baby" not in _check(scratch_db)
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_baby_style_inserts(db: Conn) -> None:
    tenant = f.tenant(db)
    style = f.product(db, tenant, "style", gender="baby")
    row = db.execute("SELECT gender, provenance ? 'gender' FROM products WHERE id = %s", [style])
    assert row.fetchone() == ("baby", True)


def _refused(db: Conn, constraint: str, write: Callable[[], object]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as caught, db.transaction():
        write()
    assert caught.value.diag.constraint_name == constraint


def test_kids_refused(db: Conn) -> None:
    tenant = f.tenant(db)
    _refused(db, "ck_products_gender", lambda: f.product(db, tenant, gender="kids"))


def test_baby_on_a_variant_is_style_owned(db: Conn) -> None:
    tenant = f.tenant(db)
    style = f.product(db, tenant, "style", gender="baby")
    plain = {"title": f.fact(), "category": f.fact()}  # the facts guard would refuse it first
    _refused(
        db,
        "ck_products_style_owned",
        lambda: f.product(db, tenant, "child", parent=style, gender="baby", provenance=plain),
    )


def test_downgrade_refused_with_a_baby_row(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, "head")
    sync_dictionary_on(scratch_db.url)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        tenant = f.tenant(conn)
        product = f.product(conn, tenant, gender="baby")
    with pytest.raises(Exception, match="downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    assert "baby" in _check(scratch_db)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        row = conn.execute("SELECT gender FROM products WHERE id = %s", [product]).fetchone()
    assert row == ("baby",)
