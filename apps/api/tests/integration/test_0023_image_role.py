"""Revision 0023_image_role (SI12 T3, AC1): the round trip, the role CHECK, the one-holder index,
the read-only grants, the job entity type and the guarded downgrade."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from . import factories as f
from .conftest import ScratchDb, sync_dictionary_on
from .integrity_support import scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

REVISION = "0023_image_role"


def _previous() -> str:
    from alembic.script import ScriptDirectory
    from app.db.migrations import alembic_config

    rev = ScriptDirectory.from_config(alembic_config()).get_revision(REVISION)
    assert rev is not None and isinstance(rev.down_revision, str)
    return rev.down_revision


def _has_role(db: ScratchDb) -> bool:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        return bool(
            scalar(
                conn,
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'images' AND column_name = 'role'",
            )
        )


def _style(db: Conn) -> tuple[UUID, UUID]:
    tenant = f.tenant(db)
    return tenant, f.product(db, tenant)


# ---- round trip ----


def test_upgrade_downgrade_upgrade_clean(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    previous = _previous()
    cfg = alembic_env(None)
    command.upgrade(cfg, previous)
    assert not _has_role(scratch_db)
    command.upgrade(cfg, REVISION)
    assert _has_role(scratch_db)
    command.upgrade(cfg, "head")
    command.check(cfg)
    command.downgrade(cfg, previous)
    assert not _has_role(scratch_db)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        definition = scalar(
            conn,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_job_runs_entity_type'",
        )
        assert "size_chart_read" not in definition
        assert scalar(conn, "SELECT to_regclass('public.images_one_role')") is None
    command.upgrade(cfg, "head")
    command.check(cfg)


def _seed_and_downgrade(alembic_env: MakeConfig, scratch_db: ScratchDb, statement: str) -> None:
    previous = _previous()
    cfg = alembic_env(None)
    command.upgrade(cfg, "head")
    sync_dictionary_on(scratch_db.url)  # the facts guard reads it when the product is written
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        tenant = f.tenant(conn)
        product = f.product(conn, tenant)
        conn.execute(statement.format(tenant=tenant, product=product))
    with pytest.raises(Exception, match="0023"):
        command.downgrade(cfg, previous)
    assert _has_role(scratch_db)


def test_downgrade_refuses_with_a_role(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    _seed_and_downgrade(
        alembic_env,
        scratch_db,
        "INSERT INTO images (tenant_id, product_id, storage_key, slot, role) "
        "VALUES ('{tenant}', '{product}', '{tenant}/images/r1.jpg', 'secondary', 'back')",
    )


def test_downgrade_refuses_with_a_read_job(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    _seed_and_downgrade(
        alembic_env,
        scratch_db,
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
        "correlation_id) VALUES ('{tenant}', 'image_compliance', 'size_chart_read', "
        "gen_random_uuid(), '{{}}'::jsonb, gen_random_uuid())",
    )


# ---- the role CHECK ----


@pytest.mark.parametrize("slot", ["main", "lifestyle", "size_chart", "packaging_label"])
def test_role_only_on_secondary(db: Conn, slot: str) -> None:
    tenant, product = _style(db)
    with pytest.raises(psycopg.errors.CheckViolation) as caught, db.transaction():
        f.image(db, tenant, product, slot=slot, role="back")
    assert caught.value.diag.constraint_name == "ck_images_role"


def test_role_vocabulary_is_closed(db: Conn) -> None:
    tenant, product = _style(db)
    for role in ("front", "", "BACK"):
        with pytest.raises(psycopg.errors.CheckViolation) as caught, db.transaction():
            f.image(db, tenant, product, role=role)
        assert caught.value.diag.constraint_name == "ck_images_role"
    assert f.image(db, tenant, product, role="back")
    assert f.image(db, tenant, product, role="close_up")
    assert f.image(db, tenant, product)  # a plain secondary keeps role null


def test_role_check_is_validated(db: Conn) -> None:
    assert scalar(db, "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_images_role'")


# ---- one holder per (product, colourway, role) ----


def test_two_live_backs_refused(db: Conn) -> None:
    tenant, product = _style(db)
    f.image(db, tenant, product, role="back")
    with pytest.raises(psycopg.errors.UniqueViolation) as caught, db.transaction():
        f.image(db, tenant, product, role="back")
    assert caught.value.diag.constraint_name == "images_one_role"


def test_deleted_holder_does_not_count(db: Conn) -> None:
    tenant, product = _style(db)
    f.image(db, tenant, product, role="back", deleted_at="2026-11-01T00:00:00Z")
    assert f.image(db, tenant, product, role="back")


def test_same_role_on_two_colourways(db: Conn) -> None:
    tenant, product = _style(db)
    red = f.colourway(db, tenant, product)
    blue = f.colourway(db, tenant, product)
    f.image(db, tenant, product, role="back", colourway_id=red)
    f.image(db, tenant, product, role="back", colourway_id=blue)
    f.image(db, tenant, product, role="back")
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        f.image(db, tenant, product, role="back", colourway_id=red)


# ---- grants and the job entity type ----


@pytest.mark.parametrize("role", ["app_ro", "app_platform_ro"])
def test_read_only_roles_read_role(db: Conn, role: str) -> None:
    assert scalar(db, "SELECT has_column_privilege(%s, 'public.images', 'role', 'SELECT')", (role,))


def test_job_entity_type_takes_size_chart_read(db: Conn) -> None:
    tenant = f.tenant(db)
    columns: dict[str, Any] = {
        "tenant_id": tenant,
        "kind": "image_compliance",
        "entity_id": uuid4(),
        "payload_json": {},
        "correlation_id": uuid4(),
    }
    assert f.insert(db, "job_runs", entity_type="size_chart_read", **columns)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        f.insert(db, "job_runs", entity_type="x", **columns)
    assert scalar(
        db, "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_job_runs_entity_type'"
    )
