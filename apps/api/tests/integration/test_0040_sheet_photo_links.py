"""Revision 0040_sheet_photo_links (G2): the single head, the round trip of the `photo_links`
table, the widened job entity list and the purge order, the column grants (no link and no hash
for the read roles), the CHECKs and the dedupe index, and the guarded downgrade."""

from __future__ import annotations

import re
import uuid
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

PREVIOUS = "0039_product_archive"
REVISION = "0040_sheet_photo_links"
ENTITY_CHECK = "ck_job_runs_entity_type"
PRIVILEGE = "SELECT has_column_privilege(%s, 'public.photo_links', %s, %s)"


def _members(conn: Conn) -> set[str]:
    definition = scalar(
        conn,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
        (ENTITY_CHECK,),
    )
    return set(re.findall(r"'([a-z_]+)'", str(definition)))


def _has_table(conn: Conn) -> bool:
    return scalar(conn, "SELECT to_regclass('public.photo_links')") is not None


def test_chained_on_0039() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_upgrade_downgrade_upgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert not _has_table(conn)
        parent = _members(conn)
        purge_before = list(scalar(conn, "SELECT purge_tenant_order()"))
    assert "photo_match" in parent and "photo_links" not in parent
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _has_table(conn)
        assert _members(conn) == parent | {"photo_links"}
        purge_after = list(scalar(conn, "SELECT purge_tenant_order()"))
        assert purge_after == ["photo_links", *purge_before]
        forced = scalar(
            conn, "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'photo_links'"
        )
        assert forced is True
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert not _has_table(conn)
        assert _members(conn) == parent
        assert list(scalar(conn, "SELECT purge_tenant_order()")) == purge_before
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_downgrade_refused_while_a_job_exists(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        tenant = f.tenant(conn)
        conn.execute(
            "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
            "correlation_id) VALUES (%s, 'image_compliance', 'photo_links', %s, '{}', %s)",
            [tenant, uuid.uuid4(), uuid.uuid4()],
        )
        conn.commit()
    with pytest.raises(Exception, match="0040 downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT version_num FROM alembic_version") == REVISION


def test_grants_keep_the_link_from_the_read_roles(db: Conn) -> None:
    for role in ("app_ro", "app_platform_ro"):
        for column in ("url", "url_hash"):
            assert not scalar(db, PRIVILEGE, (role, column, "SELECT")), (role, column)
        for column in ("status", "problem", "image_id", "product_id"):
            assert scalar(db, PRIVILEGE, (role, column, "SELECT")), (role, column)
    for column in ("status", "problem", "http_status", "image_id", "job_id", "attempts"):
        assert scalar(db, PRIVILEGE, ("app_rw", column, "UPDATE")), column
    for column in ("url", "url_hash", "product_id", "colourway_id", "tenant_id"):
        assert not scalar(db, PRIVILEGE, ("app_rw", column, "UPDATE")), column


def test_checks_and_dedupe(db: Conn) -> None:
    tenant = f.tenant(db)
    product = f.product(db, tenant, category="kurtas")
    f.photo_link(db, tenant, product, url_hash="a" * 64)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        f.photo_link(db, tenant, product, url_hash="a" * 64)
    bad: list[dict[str, Any]] = [
        {"url": "http://cdn.example.com/x.jpg"},
        {"url_hash": "not-a-hash"},
        {"position": 10},
        {"source_column": "size_chart"},
        {"status": "failed"},  # a failure needs its problem
        {"status": "saved"},  # a saved link needs its image
        {"status": "queued", "problem": "timeout"},
        {"status": "failed", "problem": "teapot"},
        {"status": "failed", "problem": "unsupported_type", "file_type": "svg"},
    ]
    for columns in bad:
        with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
            f.photo_link(db, tenant, product, **columns)
    # the dedupe treats "no colourway" as one value
    style = f.product(db, tenant, "style", size_run=["s"], category="kurtas")
    f.photo_link(db, tenant, style, url_hash="b" * 64)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        f.photo_link(db, tenant, style, url_hash="b" * 64)
