"""Revision 0027_v1_1_assist (SI14 T1, AC1-AC2): the round trip, the guarded downgrade, the nullable
category and its relaxed sourcing CHECK, `category_suggestion_valid`, the `category_review` task
kind and its one-live index, and the widened entity types (from the parent's list, read live)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0024_brief_gaps"
REVISION = "0027_v1_1_assist"
ENTITY_CHECK = "ck_job_runs_entity_type"

GOOD: dict[str, Any] = {
    "status": "suggested",
    "candidates": [
        {
            "category": "t_shirts",
            "confidence": 0.91,
            "rationale": "A crew-neck cotton tee.",
            "evidence_refs": ["text.title"],
        }
    ],
    "input_digest": "a" * 64,
    "calibration_version": "classify-cal-1",
    "job_id": str(uuid4()),
    "at": "2026-11-09T10:00:00Z",
}


def _check_def(conn: Conn, name: str) -> str:
    return str(
        scalar(
            conn, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s", (name,)
        )
    )


def _members(definition: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", definition))


def _has_table(libpq: str) -> bool:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return scalar(conn, "SELECT to_regclass('public.import_column_maps')") is not None


# ---- the chain ----


def test_single_head_on_0024() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == [REVISION]
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


# ---- round trip ----


def test_upgrade_downgrade_upgrade_keeps_parent_types(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        parent = _members(_check_def(conn, ENTITY_CHECK))
        purge_before = scalar(conn, "SELECT purge_tenant_order()")
        kinds_before = _members(_check_def(conn, "ck_human_tasks_kind"))
    assert "error_proposal" in parent  # SI13's member is in the chain
    command.upgrade(cfg, REVISION)
    assert _has_table(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        after = _members(_check_def(conn, ENTITY_CHECK))
        assert after == parent | {"category_suggestion", "column_map"}
        assert scalar(conn, "SELECT purge_tenant_order()") != purge_before
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert not _has_table(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _members(_check_def(conn, ENTITY_CHECK)) == parent
        assert scalar(conn, "SELECT purge_tenant_order()") == purge_before
        assert _members(_check_def(conn, "ck_human_tasks_kind")) == kinds_before
        nullable = scalar(
            conn,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'products' AND column_name = 'category'",
        )
        assert nullable == "NO"
        sourced = _check_def(conn, "ck_products_typed_facts_sourced")
        assert "(category IS NULL)" not in sourced and "'category'" in sourced
    command.upgrade(cfg, "head")
    command.check(cfg)


DOWNGRADE_REFUSALS = {
    "null_category": (
        "UPDATE products SET category = NULL, provenance = provenance - 'category' "
        "WHERE id = {product}"
    ),
    "category_review": (
        "INSERT INTO human_tasks (tenant_id, kind, product_id, question, reason, expected_answer, "
        "resume_from) VALUES ({tenant}, 'category_review', {product}, 'Which category fits?', "
        "'The classifier is unsure.', '{{\"kind\": \"enum\"}}'::jsonb, 'generate')"
    ),
    "column_map_job": (
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
        "correlation_id) VALUES ({tenant}, 'image_compliance', 'column_map', gen_random_uuid(), "
        "'{{}}'::jsonb, gen_random_uuid())"
    ),
    "column_map_row": (
        "INSERT INTO import_column_maps (tenant_id, status, headers, headers_digest, mappings, "
        "created_by_user_id) VALUES ({tenant}, 'ready', ARRAY['sku'], repeat('0', 64), "
        "'[]'::jsonb, {user})"
    ),
}


@pytest.mark.parametrize("case", sorted(DOWNGRADE_REFUSALS))
def test_downgrade_refuses_on_data(
    alembic_env: MakeConfig, scratch_db: ScratchDb, case: str
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        # a scratch database has no dictionary: the fact guard is off for this set-up only
        conn.execute("ALTER TABLE products DISABLE TRIGGER facts_guard")
        tenant = f.tenant(conn)
        user = f.member(conn, tenant)
        product = f.product(conn, tenant)
        conn.execute(
            DOWNGRADE_REFUSALS[case].format(
                tenant=f"'{tenant}'::uuid", product=f"'{product}'::uuid", user=f"'{user}'::uuid"
            )
        )
        conn.commit()
        conn.execute("ALTER TABLE products ENABLE TRIGGER facts_guard")
        conn.commit()
    with pytest.raises(Exception, match="0027 downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT version_num FROM alembic_version") == REVISION


# ---- the nullable category ----


def test_null_category_product_inserts(db: Conn) -> None:
    tenant = f.tenant(db)
    product = f.product(db, tenant, category=None, provenance={"title": f.fact()})
    assert scalar(db, "SELECT category FROM products WHERE id = %s", (product,)) is None


def test_set_category_still_needs_its_source(db: Conn) -> None:
    tenant = f.tenant(db)
    product = f.product(db, tenant, category=None, provenance={"title": f.fact()})
    expect_sqlstate(
        db,
        "23514",
        "UPDATE products SET category = 't_shirts' WHERE id = %s",
        (product,),
        constraint="ck_products_typed_facts_sourced",
    )


# ---- category_suggestion_valid ----


def _valid(db: Conn, value: Any) -> bool:
    return bool(scalar(db, "SELECT category_suggestion_valid(%s::jsonb)", (json.dumps(value),)))


def _candidate(**over: Any) -> dict[str, Any]:
    return {**GOOD["candidates"][0], **over}


def test_suggestion_valid_accepts_good_values(db: Conn) -> None:
    assert _valid(db, GOOD)
    assert _valid(db, {"status": "unavailable", "candidates": [], "at": "2026-11-09T10:00:00Z"})
    assert _valid(db, {**GOOD, "job_id": None, "calibration_version": None, "input_digest": None})


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "candidates": [_candidate()] * 4},
        {**GOOD, "candidates": [_candidate(confidence=1.5)]},
        {**GOOD, "candidates": [_candidate(confidence="NaN")]},
        {**GOOD, "candidates": [_candidate(rationale="x" * 301)]},
        {**GOOD, "candidates": [_candidate(category="T-Shirts")]},
        {**GOOD, "candidates": [_candidate(evidence_refs=["x"] * 13)]},
        {**GOOD, "candidates": [_candidate(extra=1)]},
        {**GOOD, "input_digest": "z" * 64},
        {**GOOD, "status": "guessed"},
        {**GOOD, "job_id": "not-a-uuid"},
        {**GOOD, "calibration_version": "x" * 33},
        {**GOOD, "unknown": True},
        {k: v for k, v in GOOD.items() if k != "at"},
        [GOOD],
    ],
)
def test_suggestion_valid_rejects(db: Conn, bad: Any) -> None:
    assert not _valid(db, bad)


def test_products_check_refuses_a_bad_suggestion(db: Conn) -> None:
    tenant = f.tenant(db)
    product = f.product(db, tenant)
    db.execute(
        "UPDATE products SET category_suggestion = %s::jsonb WHERE id = %s",
        (json.dumps(GOOD), product),
    )
    expect_sqlstate(
        db,
        "23514",
        'UPDATE products SET category_suggestion = \'{"status": "x"}\'::jsonb WHERE id = %s',
        (product,),
        constraint="ck_products_category_suggestion",
    )


# ---- category_review ----


def _review(db: Conn, tenant: Any, product: Any, **over: Any) -> Any:
    return f.human_task(
        db,
        tenant,
        product_id=product,
        kind="category_review",
        target_path=None,
        question="Which category fits this product?",
        reason="The classifier is not sure.",
        expected_answer={"kind": "enum", "allowed": ["t_shirts"]},
        **over,
    )


def test_one_live_category_review_per_product(db: Conn) -> None:
    tenant = f.tenant(db)
    product = f.product(db, tenant)
    first = _review(db, tenant, product)
    expect_sqlstate(
        db,
        "23505",
        "INSERT INTO human_tasks (tenant_id, kind, product_id, question, reason, expected_answer, "
        "resume_from) VALUES (%s, 'category_review', %s, 'Which category fits?', "
        "'The classifier is unsure.', '{}'::jsonb, 'generate')",
        (tenant, product),
    )
    user = f.member(db, tenant)
    db.execute(
        "UPDATE human_tasks SET status = 'answered', answer = '{\"category\": \"t_shirts\"}', "
        "answered_by_user_id = %s, answered_at = now() WHERE id = %s",
        (user, first),
    )
    db.execute(
        "UPDATE human_tasks SET status = 'applied', applied_at = now() WHERE id = %s", (first,)
    )
    assert _review(db, tenant, product)


def test_category_review_takes_no_target_path(db: Conn) -> None:
    tenant = f.tenant(db)
    product = f.product(db, tenant)
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO human_tasks (tenant_id, kind, product_id, target_path, question, reason, "
        "expected_answer, resume_from) VALUES (%s, 'category_review', %s, 'facts.fabric', "
        "'Which category fits?', 'The classifier is unsure.', '{}'::jsonb, 'generate')",
        (tenant, product),
        constraint="ck_human_tasks_category_review",
    )


# ---- entity types ----


def test_column_map_entity_type_inserts_and_foo_refused(db: Conn) -> None:
    tenant = f.tenant(db)
    for entity in ("column_map", "category_suggestion"):
        assert f.insert(
            db,
            "job_runs",
            tenant_id=tenant,
            kind="image_compliance",
            entity_type=entity,
            entity_id=uuid4(),
            payload_json={},
            correlation_id=uuid4(),
        )
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
        "correlation_id) VALUES (%s, 'image_compliance', 'foo', gen_random_uuid(), '{}'::jsonb, "
        "gen_random_uuid())",
        (tenant,),
        constraint=ENTITY_CHECK,
    )
    for name in (ENTITY_CHECK, "ck_human_tasks_kind", "ck_products_category_suggestion"):
        assert scalar(db, "SELECT convalidated FROM pg_constraint WHERE conname = %s", (name,))
