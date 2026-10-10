"""Revision 0009_analytics_trace (S23 T1): `job_runs.traceparent` with its format CHECK, and the four
analytics read indexes built CONCURRENTLY (an INVALID leftover is replaced). Test names are
id-neutral: the revision id moves when the ledger is re-parented at rebase."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from . import factories as f
from .conftest import REPO_ROOT, ScratchDb, expect_sqlstate
from .ops_support import job_run

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

MakeConfig = Callable[[Path | None], Config]
Conn = psycopg.Connection[tuple[Any, ...]]
REVISION = "0009_analytics_trace"
INDEXES = (
    "ix_listing_versions_tenant_id_created_at_generated",
    "ix_review_decisions_tenant_id_decided_at",
    "ix_validation_runs_tenant_id_created_at_first_pass",
    "ix_listing_submissions_tenant_id_responded_at",
)
CHECK = "ck_job_runs_traceparent_format"
VALID = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
LEDGER_ROW = re.compile(r"^\| `(\d{4}[a-z]?_[a-z_]+)` \|", re.M)


def _previous() -> str:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config()).get_revision(REVISION)
    assert script is not None and isinstance(script.down_revision, str)
    return script.down_revision


def _ledger() -> list[str]:
    text = (REPO_ROOT / "docs/canon/CANON.md").read_text(encoding="utf-8")
    section = text.split("## Migration ledger", 1)[1].split("\n## ", 1)[0]
    return LEDGER_ROW.findall(section)


def _columns(conn: Conn) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'job_runs'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _indexes(conn: Conn) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = ANY(%s)",
        [list(INDEXES)],
    ).fetchall()
    return {str(r[0]): bool(r[1]) for r in rows}


def test_mig_up_down_up(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    previous = _previous()
    command.upgrade(cfg, previous)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        before = _columns(conn)
        assert _indexes(conn) == {}
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _columns(conn) == before | {"traceparent"}
        assert _indexes(conn) == dict.fromkeys(INDEXES, True)
    command.downgrade(cfg, previous)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _columns(conn) == before
        assert _indexes(conn) == {}
        assert conn.execute(
            "SELECT count(*) FROM pg_constraint WHERE conname = %s", [CHECK]
        ).fetchone() == (0,)
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_mig_alembic_check_no_drift(alembic_env: MakeConfig) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_mig_recovers_invalid_index(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    """An interrupted CONCURRENTLY build leaves an INVALID index under the planned name; the
    upgrade drops it and builds a valid one."""
    cfg = alembic_env(None)
    command.upgrade(cfg, _previous())
    name = INDEXES[1]
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        conn.execute("CREATE TABLE public.s23_dup (x int)")
        conn.execute("INSERT INTO public.s23_dup VALUES (1), (1)")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(f"CREATE UNIQUE INDEX CONCURRENTLY {name} ON public.s23_dup (x)")
        assert _indexes(conn) == {name: False}
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert _indexes(conn) == dict.fromkeys(INDEXES, True)
        table = conn.execute(
            "SELECT indrelid::regclass::text FROM pg_index WHERE indexrelid = %s::regclass",
            [f"public.{name}"],
        ).fetchone()
        assert table == ("review_decisions",)
        conn.execute("DROP TABLE public.s23_dup")


def test_mig_indexes_valid(db: Conn) -> None:
    assert _indexes(db) == dict.fromkeys(INDEXES, True)
    definitions = {
        str(r[0]): str(r[1])
        for r in db.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY(%s)", [list(INDEXES)]
        ).fetchall()
    }
    assert "WHERE (origin = ANY" in definitions[INDEXES[0]]
    assert "INCLUDE (decision)" in definitions[INDEXES[1]]
    assert "INCLUDE (passed, marketplace)" in definitions[INDEXES[2]]
    assert "INCLUDE (outcome, marketplace)" in definitions[INDEXES[3]]


REJECTS = {
    "uppercase_hex": "00-0AF7651916CD43DD8448EB211C80319C-B7AD6B7169203331-01",
    "version_01": "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    "flags_02": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-02",
    "short_span": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b716920333-01",
    "trailing_space": VALID + " ",
    "empty": "",
}


@pytest.mark.parametrize("case", sorted(REJECTS))
def test_mig_traceparent_check_rejects(db: Conn, case: str) -> None:
    tenant = f.tenant(db)
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
        "correlation_id, traceparent) VALUES (%s, 'bulk_import', 'import', gen_random_uuid(), "
        "'{}', gen_random_uuid(), %s)",
        [tenant, REJECTS[case]],
        constraint=CHECK,
    )


def test_mig_traceparent_accepts_null_and_valid(db: Conn) -> None:
    tenant = f.tenant(db)
    for value in (None, VALID, VALID[:-2] + "00"):
        job = job_run(db, tenant, traceparent=value)
        row = db.execute("SELECT traceparent FROM job_runs WHERE id = %s", [job]).fetchone()
        assert row == (value,)


def test_mig_down_revision_is_ledger_head() -> None:
    ledger = _ledger()
    assert REVISION in ledger
    assert _previous() == ledger[ledger.index(REVISION) - 1]


def test_mig_single_head() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, heads
    assert REVISION in {r.revision for r in script.walk_revisions("base", heads[0])}


@pytest.mark.parametrize("role", ["app_ro", "app_platform_ro"])
def test_mig_readonly_roles_select_traceparent(db: Conn, role: str) -> None:
    row = db.execute(
        "SELECT has_column_privilege(%s, 'public.job_runs', 'traceparent', 'SELECT')", [role]
    ).fetchone()
    assert row == (True,)


def test_mig_terminal_row_guard_unchanged(db: Conn) -> None:
    job = job_run(
        db,
        f.tenant(db),
        status="succeeded",
        started_at="2026-10-06T00:00:00Z",
        finished_at="2026-10-06T01:00:00Z",
    )
    expect_sqlstate(db, "GRD01", "UPDATE job_runs SET traceparent = %s WHERE id = %s", [VALID, job])
