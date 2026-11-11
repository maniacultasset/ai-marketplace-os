"""Revision 0030_candidate_cache_accepted (ADR-0030 amendment, 11 Nov 2026): the single head, the
cache index without accepted rows (an accepted row no longer holds its cache key), the model's
predicate matching the database, and the guarded downgrade."""

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
from .conftest import ScratchDb, sync_dictionary_on
from .integrity_support import scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0029_admin_google_link"
REVISION = "0030_candidate_cache_accepted"
INDEX = "uq_image_candidates_cache"
KEY = {"source_hash": "d" * 64, "generator_key": "fix/v1"}


def _definition(conn: Conn) -> str:
    return str(scalar(conn, "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (INDEX,)))


def _accepted(conn: Conn) -> tuple[UUID, UUID, UUID]:
    """An accepted `fix` candidate on KEY: (tenant, product, source image)."""
    tenant = f.tenant(conn)
    product = f.product(conn, tenant)
    source = f.image(conn, tenant, product, slot="main")
    made = f.image(conn, tenant, product, slot="secondary")
    f.image_candidate(
        conn,
        tenant,
        product,
        source,
        status="accepted",
        storage_key="k0",
        accepted_image_id=made,
        **KEY,
    )
    return tenant, product, source


def test_single_head_on_0029() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == [REVISION]
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_index_matches_the_model_and_leaves_accepted_rows_out(db: Conn) -> None:
    from app.db.models.catalog_assets import CANDIDATE_CACHE_WHERE

    definition = _definition(db)
    assert "UNIQUE" in definition and "status <> 'accepted'::text" in definition
    assert "'source_changed'::text" in definition
    assert "status <> 'accepted'" in CANDIDATE_CACHE_WHERE
    valid = scalar(
        db,
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = %s",
        (INDEX,),
    )
    assert valid is True
    assert scalar(db, "SELECT to_regclass('public.uq_image_candidates_cache_next')") is None


def test_an_accepted_row_no_longer_holds_its_cache_key(db: Conn) -> None:
    tenant, product, source = _accepted(db)
    f.image_candidate(db, tenant, product, source, status="ready", storage_key="k1", **KEY)
    with pytest.raises(psycopg.errors.UniqueViolation) as caught, db.transaction():
        f.image_candidate(db, tenant, product, source, **KEY)
    assert caught.value.diag.constraint_name == INDEX


def test_upgrade_downgrade_upgrade_and_the_guarded_downgrade(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert "accepted" not in _definition(conn)
    command.upgrade(cfg, "head")
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert "status <> 'accepted'::text" in _definition(conn)
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert "accepted" not in _definition(conn)
    command.upgrade(cfg, "head")
    sync_dictionary_on(scratch_db.url)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        tenant, product, source = _accepted(conn)
        f.image_candidate(conn, tenant, product, source, **KEY)  # a new queued row, same key
    with pytest.raises(Exception, match="0030 downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert "status <> 'accepted'::text" in _definition(conn)
