"""Revision 0018_image_generation (SI7 T3, ADR-0030 proposed): the round trip, the candidate CHECKs,
the partial cache index, RLS and grants, image provenance (write-once), the job entity type, the
audit allow-list and the purge order."""

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
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import as_app_rw, as_migrator, scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0017_team_access"
REVISION = "0018_image_generation"
FORCE_DATA = "SELECT purge_tenant(%s, 'data', force => true)"
FUNCTIONS = ("audit_allowlist", "purge_tenant_order")


def _functiondefs(db: ScratchDb) -> dict[str, str]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        return {
            name: str(
                scalar(
                    conn,
                    "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' AND p.proname = %s",
                    (name,),
                )
            )
            for name in FUNCTIONS
        }


def _has_table(db: ScratchDb) -> bool:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        return bool(scalar(conn, "SELECT to_regclass('public.image_candidates') IS NOT NULL"))


def _source(db: Conn) -> tuple[UUID, UUID, UUID]:
    tenant = f.tenant(db)
    product = f.product(db, tenant)
    return tenant, product, f.image(db, tenant, product, slot="main")


# ---- round trip ----


def test_upgrade_downgrade_upgrade_clean(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    before = _functiondefs(scratch_db)
    assert not _has_table(scratch_db)
    command.upgrade(cfg, REVISION)
    assert _has_table(scratch_db)
    assert _functiondefs(scratch_db) != before
    command.upgrade(cfg, "head")  # 0020, 0019, 0021 sit on top: `check` at the head
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert not _has_table(scratch_db)
    assert _functiondefs(scratch_db) == before
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        cols = scalar(
            conn,
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'images' "
            "AND column_name IN ('origin', 'parent_image_id', 'candidate_id')",
        )
        assert cols == 0
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_on_the_single_head_chain() -> None:
    from alembic.script import ScriptDirectory
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, heads  # 0020 (SI9), 0019 (SI8), then 0021 (SI10) sit on top
    assert REVISION in {rev.revision for rev in script.walk_revisions("base", heads[0])}


# ---- CHECKs ----


def test_failure_reason_iff_failed(db: Conn) -> None:
    tenant, product, source = _source(db)
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO image_candidates (tenant_id, product_id, source_image_id, kind, status, "
        "source_hash, generator_key) VALUES (%s, %s, %s, 'fix', 'failed', %s, 'fix/v1')",
        (tenant, product, source, "a" * 64),
        constraint="ck_image_candidates_failed",
    )
    candidate = f.image_candidate(db, tenant, product, source)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE image_candidates SET failure_reason = 'fidelity' WHERE id = %s",
        (candidate,),
        constraint="ck_image_candidates_failed",
    )


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("kind", "lifestyle", "ck_image_candidates_kind"),
        ("source_hash", "A" * 64, "ck_image_candidates_source_hash"),
        ("source_hash", "a" * 63, "ck_image_candidates_source_hash"),
        ("cost_usd", -1, "ck_image_candidates_cost_usd"),
        ("generator_key", "", "ck_image_candidates_generator_key"),
        ("served_provider", "x" * 65, "ck_image_candidates_served_provider"),
        ("mime_type", "image/png", "ck_image_candidates_mime_type"),
        ("status", "done", "ck_image_candidates_status"),
    ],
)
def test_bad_values_refused(db: Conn, column: str, value: Any, constraint: str) -> None:
    tenant, product, source = _source(db)
    with pytest.raises(psycopg.errors.CheckViolation) as caught, db.transaction():
        f.image_candidate(db, tenant, product, source, **{column: value})
    assert caught.value.diag.constraint_name == constraint


def test_source_matches_kind(db: Conn) -> None:
    tenant, product, source = _source(db)
    with pytest.raises(psycopg.errors.CheckViolation) as caught, db.transaction():
        f.image_candidate(db, tenant, product, source, kind="size_chart")
    assert caught.value.diag.constraint_name == "ck_image_candidates_source"
    with pytest.raises(psycopg.errors.CheckViolation) as caught2, db.transaction():
        f.image_candidate(db, tenant, product, None, kind="fix")
    assert caught2.value.diag.constraint_name == "ck_image_candidates_source"


def test_ready_needs_an_object_and_accepted_needs_an_image(db: Conn) -> None:
    tenant, product, source = _source(db)
    candidate = f.image_candidate(db, tenant, product, source)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE image_candidates SET status = 'ready' WHERE id = %s",
        (candidate,),
        constraint="ck_image_candidates_stored",
    )
    expect_sqlstate(
        db,
        "23514",
        "UPDATE image_candidates SET status = 'accepted', storage_key = 'k' WHERE id = %s",
        (candidate,),
        constraint="ck_image_candidates_accepted",
    )


# ---- the cache index ----


def test_cache_index_refuses_a_second_ready_row(db: Conn) -> None:
    tenant, product, source = _source(db)
    key = {"source_hash": "b" * 64, "generator_key": "fix/v1"}
    f.image_candidate(db, tenant, product, source, status="ready", storage_key="k1", **key)
    with pytest.raises(psycopg.errors.UniqueViolation) as caught, db.transaction():
        f.image_candidate(db, tenant, product, source, **key)
    assert caught.value.diag.constraint_name == "uq_image_candidates_cache"


def test_cache_index_skips_retryable_failures(db: Conn) -> None:
    tenant, product, source = _source(db)
    key = {"source_hash": "c" * 64, "generator_key": "fix/v1"}
    for reason in ("model_failed", "model_failed", "credit_used_up", "budget", "source_changed"):
        f.image_candidate(
            db, tenant, product, source, status="failed", failure_reason=reason, **key
        )
    f.image_candidate(db, tenant, product, source, **key)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        f.image_candidate(
            db, tenant, product, source, status="failed", failure_reason="fidelity", **key
        )


# ---- RLS and grants ----


def test_rls_hides_other_tenants_and_refuses_their_inserts(db: Conn) -> None:
    tenant, product, source = _source(db)
    candidate = f.image_candidate(db, tenant, product, source)
    other = f.tenant(db)
    with as_app_rw(db, other):
        assert scalar(db, "SELECT count(*) FROM image_candidates WHERE id = %s", (candidate,)) == 0
        expect_sqlstate(
            db,
            "42501",
            "INSERT INTO image_candidates (tenant_id, product_id, source_image_id, kind, "
            "source_hash, generator_key) VALUES (%s, %s, %s, 'fix', %s, 'fix/v1')",
            (tenant, product, source, "d" * 64),
        )
    with as_app_rw(db, tenant):
        assert scalar(db, "SELECT count(*) FROM image_candidates WHERE id = %s", (candidate,)) == 1


@pytest.mark.parametrize("column", ["kind", "source_hash", "product_id", "generator_key"])
def test_app_rw_cannot_update_write_once_columns(db: Conn, column: str) -> None:
    tenant, product, source = _source(db)
    candidate = f.image_candidate(db, tenant, product, source)
    with as_app_rw(db, tenant):
        expect_sqlstate(
            db,
            "42501",
            f"UPDATE image_candidates SET {column} = {column} WHERE id = %s",  # noqa: S608
            (candidate,),
        )
        db.execute("UPDATE image_candidates SET status = 'queued' WHERE id = %s", (candidate,))


def test_read_only_roles_never_read_source_hash(db: Conn) -> None:
    tenant, product, source = _source(db)
    f.image_candidate(db, tenant, product, source)
    db.execute("SET ROLE app_ro")
    try:
        expect_sqlstate(db, "42501", "SELECT source_hash FROM image_candidates")
        db.execute("SELECT id, kind, status, cost_usd FROM image_candidates").fetchall()
    finally:
        db.execute("RESET ROLE")


# ---- image provenance ----


def test_generated_image_needs_a_candidate(db: Conn) -> None:
    tenant, product, source = _source(db)
    with pytest.raises(psycopg.errors.CheckViolation) as caught, db.transaction():
        f.image(db, tenant, product, origin="generated")
    assert caught.value.diag.constraint_name == "ck_images_origin_candidate"
    with pytest.raises(psycopg.errors.CheckViolation) as caught2, db.transaction():
        f.image(db, tenant, product, candidate_id=uuid4())
    assert caught2.value.diag.constraint_name == "ck_images_origin_candidate"
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        f.image(db, tenant, product, origin="ai", candidate_id=uuid4())
    assert f.image(
        db, tenant, product, origin="rendered", candidate_id=uuid4(), parent_image_id=source
    )


def test_provenance_is_write_once(db: Conn) -> None:
    tenant, product, source = _source(db)
    made = f.image(
        db, tenant, product, origin="rendered", candidate_id=uuid4(), parent_image_id=source
    )
    for statement in (
        "UPDATE images SET origin = 'generated' WHERE id = %s",
        "UPDATE images SET candidate_id = gen_random_uuid() WHERE id = %s",
        f"UPDATE images SET parent_image_id = '{uuid4()}' WHERE id = %s",  # noqa: S608
    ):
        expect_sqlstate(db, "GRD01", statement, (made,))
    db.execute("UPDATE images SET parent_image_id = NULL WHERE id = %s", (made,))
    db.execute("UPDATE images SET position = 3 WHERE id = %s", (made,))


def test_default_origin_is_seller(db: Conn) -> None:
    _, _, source = _source(db)
    assert scalar(db, "SELECT origin FROM images WHERE id = %s", (source,)) == "seller"


# ---- job entity type, audit allow-list, purge ----


def test_job_entity_type_takes_image_candidate(db: Conn) -> None:
    definition = scalar(
        db,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_job_runs_entity_type'",
    )
    assert "image_candidate" in definition
    assert scalar(
        db,
        "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_job_runs_entity_type'",
    )


def test_audit_allowlist_for_candidates(db: Conn) -> None:
    row = db.execute("SELECT audit_allowlist('image_candidate')").fetchone()
    assert row is not None and set(row[0]) == {"kind", "status"}


def test_purge_deletes_candidates_first(db: Conn) -> None:
    order = db.execute("SELECT purge_tenant_order()").fetchone()
    assert order is not None and order[0][0] == "image_candidates"
    tenant, product, source = _source(db)
    run = f.agent_run(db, tenant, agent_name="image", trigger="vision")
    candidate = f.image_candidate(
        db, tenant, product, source, kind="white_background", agent_run_id=run
    )
    made = f.image(
        db, tenant, product, origin="generated", parent_image_id=source, candidate_id=candidate
    )
    db.execute(
        "UPDATE image_candidates SET status = 'accepted', storage_key = 'k', accepted_image_id = %s "
        "WHERE id = %s",
        (made, candidate),
    )
    with as_migrator(db):
        db.execute(FORCE_DATA, (tenant,))
    assert scalar(db, "SELECT count(*) FROM image_candidates WHERE tenant_id = %s", (tenant,)) == 0
    assert scalar(db, "SELECT count(*) FROM images WHERE tenant_id = %s", (tenant,)) == 0
