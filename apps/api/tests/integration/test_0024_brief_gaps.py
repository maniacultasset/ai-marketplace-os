"""Revision 0024_brief_gaps (SI13 T1, AC19): the round trip, the guarded downgrade, the unchanged
edge set, the widened CHECKs (from the single head's lists) and `marketplace_error_proposals`:
its CHECKs, key, grants and the drafting guard (the worker never approves)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import as_app_rw, as_migrator, scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

# Chain 0023 -> 0025 -> 0026 -> 0024: re-chained onto main's head 0026 at the 2026-11-09 merge
# (0023_image_role at build); the CHECK lists and the allowlist body 0024 restores are 0023's and
# 0022's, which 0025 and 0026 leave alone.
PREVIOUS = "0026_invitee_email_unverified"
REVISION = "0024_brief_gaps"
EDGES_SQL = (
    "SELECT from_status, to_status, event, actor, requires_current_approval, voids_approval, "
    "allowed_reasons, requires_from_reason FROM listing_status_transitions "
    "ORDER BY from_status, to_status, event"
)
INSERT = (
    "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status, tries) "
    "VALUES (%s, 'code', %s, %s, %s) RETURNING id"
)
TEXT = "The barcode is linked to a different product on Amazon."
FIX = "Check the barcode and send the right one for this product."


def _has_table(libpq: str) -> bool:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return scalar(conn, "SELECT to_regclass('public.marketplace_error_proposals')") is not None


def _edges(libpq: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return conn.execute(EDGES_SQL).fetchall()


def _check_def(conn: Conn, name: str) -> str:
    return str(
        scalar(
            conn, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s", (name,)
        )
    )


# ---- the chain ----


def test_single_head_on_0026() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    # SI14's 0027_v1_1_assist chains on 0024 (test_0027_v1_1_assist checks the head)
    assert len(script.get_heads()) == 1
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


# ---- round trip ----


def test_upgrade_downgrade_upgrade_clean(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    assert not _has_table(scratch_db.libpq)
    before = _edges(scratch_db.libpq)
    command.upgrade(cfg, REVISION)
    assert _has_table(scratch_db.libpq)
    assert _edges(scratch_db.libpq) == before  # P:AC19: 0024 adds no edge (CP-D19)
    command.upgrade(cfg, "head")  # the ORM is the head's (0027 on top, SI14)
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert not _has_table(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert "error_proposal" not in _check_def(conn, "ck_job_runs_entity_type")
        assert "size_chart_read" in _check_def(conn, "ck_job_runs_entity_type")
        assert "fill_variant_axis" not in _check_def(conn, "ck_listing_repairs_rule")
        body = str(scalar(conn, "SELECT prosrc FROM pg_proc WHERE proname = 'audit_allowlist'"))
        assert "marketplace_error_proposal" not in body and "listing_repair" in body
    command.upgrade(cfg, "head")
    command.check(cfg)


def _seed_and_refuse(alembic_env: MakeConfig, scratch_db: ScratchDb, statement: str) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        tenant = f.tenant(conn)
        conn.execute(statement.format(tenant=tenant))
        conn.commit()
    with pytest.raises(Exception, match="0024 downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    assert _has_table(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT version_num FROM alembic_version") == REVISION


def test_downgrade_refuses_with_a_proposal(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    _seed_and_refuse(
        alembic_env,
        scratch_db,
        "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status) "
        "VALUES ('amazon', 'code', '99999', 'pending')",
    )


def test_downgrade_refuses_with_an_error_proposal_job(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    _seed_and_refuse(
        alembic_env,
        scratch_db,
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
        "correlation_id) VALUES ('{tenant}', 'brand_embed', 'error_proposal', "
        "gen_random_uuid(), '{{}}'::jsonb, gen_random_uuid())",
    )


def test_downgrade_refuses_with_a_fill_variant_axis_repair(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        conn.execute("SET session_replication_role = replica")  # no FK or guard on set-up rows
        conn.execute(
            "INSERT INTO listing_repairs (tenant_id, listing_id, marketplace, field_path, "
            "attribute, rule, issue_key, source_digest, to_digest, value_json) VALUES "
            "(gen_random_uuid(), gen_random_uuid(), 'amazon', 'attributes.size', 'size_name', "
            "'fill_variant_axis', 'k', sha256('a'), sha256('b'), '[]'::jsonb)"
        )
        conn.commit()
    with pytest.raises(Exception, match="0024 downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT version_num FROM alembic_version") == REVISION


# ---- widened CHECKs keep the head's members ----


def test_entity_type_keeps_every_head_member(db: Conn) -> None:
    definition = _check_def(db, "ck_job_runs_entity_type")
    for member in (
        "listing",
        "import",
        "image",
        "brand_document",
        "image_candidate",
        "size_chart_read",
        "error_proposal",
    ):
        assert f"'{member}'" in definition, member
    assert scalar(
        db, "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_job_runs_entity_type'"
    )


def test_error_proposal_job_inserts(db: Conn) -> None:
    tenant = f.tenant(db)
    assert f.insert(
        db,
        "job_runs",
        tenant_id=tenant,
        kind="brand_embed",
        entity_type="error_proposal",
        entity_id=uuid4(),
        payload_json={"op": "diagnose"},
        correlation_id=uuid4(),
    )


def test_allowlist_covers_new_and_old(db: Conn) -> None:
    assert scalar(db, "SELECT audit_allowlist('marketplace_error_proposal')") == ["status"]
    assert scalar(db, "SELECT audit_allowlist('listing_repair')") == ["rule", "attribute"]


def test_repairs_rule_takes_fill_variant_axis_only(db: Conn) -> None:
    definition = _check_def(db, "ck_listing_repairs_rule")
    assert "'fill_variant_axis'" in definition and "'image_slot'" in definition
    assert "'fill_variant_axes'" not in definition
    assert scalar(
        db, "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_listing_repairs_rule'"
    )


# ---- marketplace_error_proposals: CHECKs and key ----


@contextmanager
def _migrator(db: Conn) -> Iterator[Conn]:
    with as_migrator(db) as conn:
        yield conn


def _row(db: Conn, raw: str = "99999", status: str = "pending", tries: int = 0) -> UUID:
    found = db.execute(INSERT, ("amazon", raw, status, tries)).fetchone()
    assert found is not None
    return UUID(str(found[0]))


@pytest.mark.parametrize(
    ("statement", "params", "constraint"),
    [
        (INSERT, ("amazon", "99 999", "pending", 0), "ck_marketplace_error_proposals_raw_value"),
        (INSERT, ("ebay", "99999", "pending", 0), "ck_marketplace_error_proposals_marketplace"),
        (INSERT, ("amazon", "99999", "pending", 4), "ck_marketplace_error_proposals_tries"),
        (INSERT, ("amazon", "99999", "proposed", 0), "ck_marketplace_error_proposals_text"),
        (INSERT, ("amazon", "99999", "done", 0), "ck_marketplace_error_proposals_status"),
        (
            "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status, "
            "plain_cause, suggested_fix, lane_class) VALUES ('amazon', 'code', '9', 'proposed', "
            "%s, %s, 'seller')",
            ("x" * 301, FIX),
            "ck_marketplace_error_proposals_plain_cause",
        ),
        (
            "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status, "
            "plain_cause) VALUES ('amazon', 'code', '9', 'invalid', %s)",
            (TEXT,),
            "ck_marketplace_error_proposals_no_text",
        ),
        (
            "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status, "
            "plain_cause, suggested_fix, lane_class) VALUES ('amazon', 'code', '9', 'approved', "
            "%s, %s, 'seller')",
            (TEXT, FIX),
            "ck_marketplace_error_proposals_review",
        ),
        (
            "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status) "
            "VALUES ('amazon', 'size', '9', 'pending')",
            None,
            "ck_marketplace_error_proposals_match_on",
        ),
    ],
)
def test_proposal_checks_refuse(db: Conn, statement: str, params: Any, constraint: str) -> None:
    with _migrator(db):  # the migrator may insert any status: the CHECK answers, not the guard
        expect_sqlstate(db, "23514", statement, params, constraint=constraint)


def test_one_row_per_code(db: Conn) -> None:
    _row(db, "77777")
    expect_sqlstate(
        db,
        "23505",
        INSERT,
        ("amazon", "77777", "pending", 0),
        constraint="uq_marketplace_error_proposals_key",
    )


# ---- grants and the drafting guard ----


def test_app_rw_inserts_pending_only(db: Conn) -> None:
    with as_app_rw(db):
        assert _row(db, "11111")
        expect_sqlstate(db, "GRD01", INSERT, ("amazon", "11112", "proposed", 0))
        expect_sqlstate(db, "GRD01", INSERT, ("amazon", "11113", "pending", 1))


def test_app_rw_drafts_but_never_approves(db: Conn) -> None:
    with as_app_rw(db):
        pid = _row(db, "22222")
        expect_sqlstate(
            db,
            "GRD01",
            "UPDATE marketplace_error_proposals SET status = 'approved' WHERE id = %s",
            (pid,),
        )
        expect_sqlstate(
            db,
            "42501",
            "UPDATE marketplace_error_proposals SET reviewed_by = gen_random_uuid() WHERE id = %s",
            (pid,),
        )
        expect_sqlstate(
            db, "42501", "DELETE FROM marketplace_error_proposals WHERE id = %s", (pid,)
        )
        db.execute(
            "UPDATE marketplace_error_proposals SET status = 'proposed', plain_cause = %s, "
            "suggested_fix = %s, lane_class = 'seller' WHERE id = %s",
            (TEXT, FIX, pid),
        )
        expect_sqlstate(  # a proposed row never goes back to drafting
            db,
            "GRD01",
            "UPDATE marketplace_error_proposals SET status = 'pending' WHERE id = %s",
            (pid,),
        )


def test_app_rw_retries_a_failed_row(db: Conn) -> None:
    with as_app_rw(db):
        pid = _row(db, "33333")
        db.execute(
            "UPDATE marketplace_error_proposals SET status = 'failed', tries = 1, "
            "failure_reason = 'model_failed' WHERE id = %s",
            (pid,),
        )
        db.execute(
            "UPDATE marketplace_error_proposals SET status = 'pending', failure_reason = NULL "
            "WHERE id = %s",
            (pid,),
        )
        assert scalar(
            db, "SELECT status FROM marketplace_error_proposals WHERE id = %s", (pid,)
        ) == ("pending")


def _proposed(db: Conn, raw: str) -> UUID:
    with as_app_rw(db):
        pid = _row(db, raw)
        db.execute(
            "UPDATE marketplace_error_proposals SET status = 'proposed', plain_cause = %s, "
            "suggested_fix = %s, lane_class = 'seller' WHERE id = %s",
            (TEXT, FIX, pid),
        )
    return pid


def test_migrator_reviews_once(db: Conn) -> None:
    pid = _proposed(db, "44444")
    reviewer = f.user(db)
    with _migrator(db):
        db.execute(
            "UPDATE marketplace_error_proposals SET status = 'approved', reviewed_by = %s, "
            "reviewed_at = now(), error_map_version = 1 WHERE id = %s",
            (reviewer, pid),
        )
        expect_sqlstate(
            db,
            "GRD01",
            "UPDATE marketplace_error_proposals SET status = 'rejected' WHERE id = %s",
            (pid,),
        )
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "GRD01",
            "UPDATE marketplace_error_proposals SET tries = 1 WHERE id = %s",
            (pid,),
        )


def test_migrator_never_drafts(db: Conn) -> None:
    with _migrator(db):
        pid = _row(db, "55555")
        expect_sqlstate(
            db,
            "GRD01",
            "UPDATE marketplace_error_proposals SET status = 'failed' WHERE id = %s",
            (pid,),
        )


def test_platform_reader_selects_only(db: Conn) -> None:
    _row(db, "66666")
    db.execute("SET ROLE app_platform_ro")
    try:
        assert scalar(db, "SELECT count(*) FROM marketplace_error_proposals") >= 1
        expect_sqlstate(db, "42501", INSERT, ("amazon", "66667", "pending", 0))
    finally:
        db.execute("RESET ROLE")
