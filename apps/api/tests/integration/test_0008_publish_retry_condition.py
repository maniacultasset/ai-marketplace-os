"""Revision 0008_publish_retry_condition (S21 T1; PLAN D1; DATABASE.md §5.3 `ls_guard` :875): the
`retryable_condition` justification also accepts a previous `partially_accepted` or `rejected`
submission whose report is retryable and whose every blocking item is retryable. One
`CREATE OR REPLACE FUNCTION`, no table DDL; the downgrade restores the parent's body verbatim."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg.types.json import Jsonb

from .conftest import ScratchDb, expect_sqlstate, sync_dictionary_on
from .integrity_support import approved_flow, flow, force_state, queued_job, set_gucs, submission
from .test_0004_ls_guard import _attempt

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

MakeConfig = Callable[[Path | None], Config]
Conn = psycopg.Connection[tuple[Any, ...]]
REVISION = "0008_publish_retry_condition"
PREVIOUS = "0007_facts_guard"
LEDGER_ROW = re.compile(r"^\| `(\d{4}[a-z]?_[a-z_]+)` \|", re.M)
FUNCDEF = "SELECT pg_get_functiondef('public.ls_guard()'::regprocedure)"


def _item(retryable: bool, severity: str = "error", field: str = "content.title") -> dict[str, Any]:
    return {
        "field": field,
        "error_code": "RATE_LIMITED" if retryable else "MARKETPLACE_UNKNOWN_ERROR",
        "message": "x",
        "source": "amazon_adapter",
        "severity": severity,
        "retryable": retryable,
    }


def _publishing(db: Conn) -> tuple[Any, UUID]:
    fl = flow(db)
    set_gucs(db, user_id=fl.operator)
    approved_flow(db, fl)
    force_state(db, fl.listing_id, "approved")
    job = queued_job(db, fl, "listing_publish")
    force_state(db, fl.listing_id, "publishing")
    return fl, job


def _resolve(db: Conn, sub: UUID, outcome: str, report: dict[str, Any]) -> None:
    db.execute(
        "UPDATE listing_submissions SET outcome = %s, error_code = %s, error_report_json = %s, "
        "responded_at = now(), resolved_at = now() WHERE id = %s",
        (outcome, "RATE_LIMITED" if outcome == "error" else None, Jsonb(report), sub),
    )


def _retry_after(db: Conn, outcome: str, report: dict[str, Any]) -> tuple[str, list[Any]]:
    fl, job = _publishing(db)
    first = submission(db, fl, job)
    _resolve(db, first, outcome, {"status": "partial", **report})
    return _attempt(db, fl, job, first)


def test_ls_guard_retryable_partial_ok(db: Conn) -> None:
    query, params = _retry_after(
        db, "partially_accepted", {"retryable": True, "errors": [_item(True), _item(True)]}
    )
    db.execute(query, params)


def test_ls_guard_retryable_rejected_ok(db: Conn) -> None:
    query, params = _retry_after(db, "rejected", {"retryable": True, "errors": [_item(True)]})
    db.execute(query, params)


def test_ls_guard_non_retryable_partial_sub01(db: Conn) -> None:
    query, params = _retry_after(
        db, "partially_accepted", {"retryable": False, "errors": [_item(False)]}
    )
    expect_sqlstate(db, "SUB01", query, params)


def test_ls_guard_mixed_retryable_partial_sub01(db: Conn) -> None:
    """One 429 item and one user-action item: the top flag set by mistake is not enough."""
    query, params = _retry_after(
        db, "partially_accepted", {"retryable": True, "errors": [_item(True), _item(False)]}
    )
    expect_sqlstate(db, "SUB01", query, params)


def test_ls_guard_top_flag_true_item_false_sub01(db: Conn) -> None:
    query, params = _retry_after(
        db, "partially_accepted", {"retryable": True, "errors": [_item(False)]}
    )
    expect_sqlstate(db, "SUB01", query, params)


def test_ls_guard_warning_item_does_not_block(db: Conn) -> None:
    """Only blocking (`severity = error`) items must be retryable."""
    query, params = _retry_after(
        db,
        "partially_accepted",
        {"retryable": True, "errors": [_item(True), _item(False, severity="warning")]},
    )
    db.execute(query, params)


def test_ls_guard_error_retryable_still_ok(db: Conn) -> None:
    query, params = _retry_after(db, "error", {"retryable": True, "errors": []})
    db.execute(query, params)


# ---- the revision ----


def _ledger(repo_root: Path) -> list[str]:
    text = (repo_root / "docs/canon/CANON.md").read_text(encoding="utf-8")
    section = text.split("## Migration ledger", 1)[1].split("\n## ", 1)[0]
    return LEDGER_ROW.findall(section)


def test_mig_single_head() -> None:
    from app.db.migrations import alembic_config

    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) == 1, heads


def test_0008_down_revision_is_ledger_head(repo_root: Path) -> None:
    from app.db.migrations import alembic_config

    revision = ScriptDirectory.from_config(alembic_config()).get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS
    ledger = _ledger(repo_root)
    assert ledger[ledger.index(REVISION) - 1] == PREVIOUS


def _funcdef(url: str) -> str:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(FUNCDEF).fetchone()
    assert row is not None
    return str(row[0])


def test_0008_up_down_up(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    parent = _funcdef(scratch_db.libpq)
    command.upgrade(cfg, REVISION)
    widened = _funcdef(scratch_db.libpq)
    assert widened != parent and "partially_accepted', 'rejected')" in widened
    command.downgrade(cfg, PREVIOUS)
    # the downgrade restores the parent's body verbatim
    assert _funcdef(scratch_db.libpq) == parent
    command.upgrade(cfg, REVISION)
    assert _funcdef(scratch_db.libpq) == widened
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_0008_after_downgrade_partial_refused(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    """At the parent a retryable partial cannot be retried (the gap D1 closes)."""
    cfg = alembic_env(None)
    command.upgrade(cfg, "head")
    sync_dictionary_on(scratch_db.url)  # the facts guard reads the dictionary at write time
    command.downgrade(cfg, PREVIOUS)
    try:
        with psycopg.connect(scratch_db.libpq) as conn:
            query, params = _retry_after(
                conn, "partially_accepted", {"retryable": True, "errors": [_item(True)]}
            )
            expect_sqlstate(conn, "SUB01", query, params)
            conn.rollback()
    finally:
        command.upgrade(cfg, "head")
