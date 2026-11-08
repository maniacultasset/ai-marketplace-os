"""0025_jobruns_budget_idx (V1 hardening #10): the two partial job_runs indexes the AI budget
admission reads, matching RESERVING_KINDS, and the planner uses them for the tenant sum."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.db.migrations import alembic_config
from app.repositories.budget import RESERVING_KINDS

from .conftest import ScratchDb

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

INDEXES = ("ix_job_runs_budget_started_at", "ix_job_runs_budget_tenant_started_at")


def _definitions(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'job_runs' "
        "AND indexname = ANY(%s)",
        [list(INDEXES)],
    ).fetchall()
    return dict(rows)


def test_budget_indexes_exist_and_cover_the_reserving_kinds(db: psycopg.Connection) -> None:
    found = _definitions(db)
    assert set(found) == set(INDEXES)
    assert "(started_at) INCLUDE (tenant_id, cost_reserved_usd)" in found[INDEXES[0]]
    assert "(tenant_id, started_at) INCLUDE (cost_reserved_usd)" in found[INDEXES[1]]
    for definition in found.values():
        kinds = set(re.findall(r"'([a-z_]+)'::text", definition))
        assert kinds == set(RESERVING_KINDS), definition
    valid = db.execute(
        "SELECT bool_and(i.indisvalid) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = ANY(%s)",
        [list(INDEXES)],
    ).fetchone()
    assert valid == (True,)


def test_tenant_budget_sum_uses_a_budget_index(db: psycopg.Connection) -> None:
    db.execute("SET LOCAL enable_seqscan = off")
    plan = "\n".join(
        r[0]
        for r in db.execute(
            "EXPLAIN SELECT coalesce(sum(cost_reserved_usd), 0) FROM job_runs "
            "WHERE tenant_id = gen_random_uuid() "
            "AND kind IN ('listing_generate', 'brand_embed', 'image_compliance') "
            "AND started_at >= now() - interval '1 day' AND started_at < now()"
        ).fetchall()
    )
    # either budget index serves it (the planner picks by the table's statistics), never a scan
    assert "ix_job_runs_budget_" in plan and "Seq Scan" not in plan, plan


MakeConfig = Callable[[Path | None], Config]


def _scalar(db: ScratchDb, query: str) -> object:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        row = conn.execute(query).fetchone()
    return None if row is None else row[0]


def test_0025_and_0026_round_trip(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    """Both V1 hardening revisions chain on SI12's 0023 and downgrade cleanly (SI13's 0024 now sits
    on 0026, so the downgrade to 0023 passes through it)."""
    script = ScriptDirectory.from_config(alembic_config())
    budget, flag = script.get_revision("0025_jobruns_budget_idx"), script.get_revision(
        "0026_invitee_email_unverified"
    )
    assert budget is not None and budget.down_revision == "0023_image_role"
    assert flag is not None and flag.down_revision == "0025_jobruns_budget_idx"
    index = "SELECT to_regclass('public.ix_job_runs_budget_started_at')::text"
    column = (
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'email_unverified'"
    )
    cfg = alembic_env(None)
    command.upgrade(cfg, "head")
    command.check(cfg)
    assert _scalar(scratch_db, index) is not None and _scalar(scratch_db, column) == 1
    command.downgrade(cfg, "0023_image_role")
    assert _scalar(scratch_db, index) is None and _scalar(scratch_db, column) == 0
    command.upgrade(cfg, "head")
    command.check(cfg)
