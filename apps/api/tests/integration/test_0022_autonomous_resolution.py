"""Revision 0022_autonomous_resolution (SI11 T1-T2): the round trip, the `auto_fix` and
`corrections_exhausted` edges with their event rule, the `listings.remote_check_*` columns, and the
downgrade that refuses while an `auto_fix` row exists."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import (
    approved_flow,
    flow,
    force_state,
    next_version,
    prepare_edge,
    queued_job,
    scalar,
    set_gucs,
    transition_sql,
)
from .test_0004_transitions import SI11_EDGES

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0020_status_truth"
REVISION = "0022_autonomous_resolution"
EDGE_SQL = (
    "SELECT from_status, to_status, event, actor, requires_current_approval, voids_approval, "
    "allowed_reasons, requires_from_reason FROM listing_status_transitions "
    "WHERE from_status = %s AND to_status = %s AND event = %s"
)


def _has_column(libpq: str) -> bool:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return bool(
            scalar(
                conn,
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'listings' AND column_name = 'remote_check_status'",
            )
        )


def _edge_count(libpq: str) -> int:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return int(scalar(conn, "SELECT count(*) FROM listing_status_transitions"))


# ---- round trip ----


def test_upgrade_downgrade_upgrade_clean(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    assert not _has_column(scratch_db.libpq)
    assert _edge_count(scratch_db.libpq) == 63
    command.upgrade(cfg, REVISION)
    assert _has_column(scratch_db.libpq)
    assert _edge_count(scratch_db.libpq) == 72
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert not _has_column(scratch_db.libpq)
    assert _edge_count(scratch_db.libpq) == 63
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        body = str(
            scalar(conn, "SELECT prosrc FROM pg_proc WHERE proname = 'listings_status_events'")
        )
        assert "auto_fix" not in body
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_downgrade_refused_with_auto_fix_row(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        # the scratch database has no synced dictionary: skip the guards for the set-up rows
        conn.execute("SET session_replication_role = replica")
        fl = flow(conn)
        conn.execute(
            "UPDATE listings SET status = 'generating', last_event = 'auto_fix' WHERE id = %s",
            (fl.listing_id,),
        )
        conn.commit()
    with pytest.raises(Exception, match="downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    assert _has_column(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT version_num FROM alembic_version") == REVISION


# ---- the 9 edges ----


@pytest.mark.parametrize("key", [e[:3] for e in SI11_EDGES], ids=lambda k: "-".join(k))
def test_si11_edges_pass(db: Conn, key: tuple[str, str, str]) -> None:
    edge = db.execute(EDGE_SQL, key).fetchone()
    assert edge is not None
    _, query, params = prepare_edge(db, edge)
    db.execute(query, params)
    row = db.execute("SELECT status, last_event FROM listings WHERE id = %s", (params[-1],))
    assert row.fetchone() == (key[1], key[2])


def _in_state(db: Conn, status: str, reason: str | None = None) -> Any:
    fl = flow(db)
    set_gucs(db, user_id=fl.reviewer, job_id=uuid4())
    force_state(db, fl.listing_id, status, reason)
    return fl


def test_auto_fix_needs_queued_generate_job(db: Conn) -> None:
    fl = _in_state(db, "validated")
    query, params = transition_sql(fl.listing_id, "generating", "auto_fix")
    error = expect_sqlstate(db, "LST01", query, params)
    assert "listing_generate" in str(error)
    queued_job(db, fl, "listing_generate")
    db.execute(query, params)
    assert scalar(db, "SELECT status FROM listings WHERE id = %s", (fl.listing_id,)) == (
        "generating"
    )


def test_auto_fix_needs_a_job_actor(db: Conn) -> None:
    fl = flow(db)
    set_gucs(db, user_id=fl.reviewer)  # a user, no job
    force_state(db, fl.listing_id, "validated")
    queued_job(db, fl, "listing_generate")
    query, params = transition_sql(fl.listing_id, "generating", "auto_fix")
    expect_sqlstate(db, "LST01", query, params)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("blocked", "human_task_open"),
        ("failed", "generation_failed"),
        ("created", None),
        ("publishing", None),
    ],
)
def test_auto_fix_refused_elsewhere(db: Conn, status: str, reason: str | None) -> None:
    fl = flow(db)
    set_gucs(db, user_id=fl.reviewer, job_id=uuid4())
    if status == "publishing":
        approved_flow(db, fl)
    force_state(db, fl.listing_id, status, reason)
    queued_job(db, fl, "listing_generate")
    query, params = transition_sql(fl.listing_id, "generating", "auto_fix")
    expect_sqlstate(db, "LST01", query, params)


def test_corrections_exhausted_refused_from_approved(db: Conn) -> None:
    fl = flow(db)
    set_gucs(db, user_id=fl.reviewer, job_id=uuid4())
    approved_flow(db, fl)
    force_state(db, fl.listing_id, "approved")
    query, params = transition_sql(
        fl.listing_id,
        "blocked",
        "corrections_exhausted",
        "corrections_exhausted",
        approved_version_id=None,
    )
    expect_sqlstate(db, "LST01", query, params)


@pytest.mark.parametrize("status", ["approved", "published"])
def test_auto_fix_voids_approval_keeps_live(db: Conn, status: str) -> None:
    fl = flow(db)
    set_gucs(db, user_id=fl.reviewer, job_id=uuid4())
    approved_flow(db, fl)
    cols: dict[str, Any] = {}
    if status == "published":
        cols = {
            "published_version_id": fl.v1,
            "live_version_id": fl.v1,
            "first_published_at": "2026-10-01T00:00:00Z",
            "marketplace_state": "live",
            "live_confirmed_at": "2026-10-01T00:00:00Z",
            "last_synced_at": "2026-10-01T00:00:00Z",
        }
    force_state(db, fl.listing_id, status, **cols)
    queued_job(db, fl, "listing_generate")
    keeping = transition_sql(fl.listing_id, "generating", "auto_fix")
    expect_sqlstate(db, "LST01", *keeping)  # the approval must be voided
    db.execute(*transition_sql(fl.listing_id, "generating", "auto_fix", approved_version_id=None))
    row = db.execute(
        "SELECT approved_version_id, live_version_id FROM listings WHERE id = %s",
        (fl.listing_id,),
    ).fetchone()
    assert row == (None, fl.v1 if status == "published" else None)


# ---- listings.remote_check_* ----


def test_remote_check_all_null_ok(db: Conn) -> None:
    fl = flow(db)
    row = db.execute(
        "SELECT remote_check_version_id, remote_check_status, remote_check_at FROM listings "
        "WHERE id = %s",
        (fl.listing_id,),
    ).fetchone()
    assert row == (None, None, None)


def test_remote_check_pending_without_at_ok(db: Conn) -> None:
    fl = flow(db)
    db.execute(
        "UPDATE listings SET remote_check_version_id = %s, remote_check_status = 'pending' "
        "WHERE id = %s",
        (fl.v1, fl.listing_id),
    )
    db.execute(
        "UPDATE listings SET remote_check_status = 'clean', remote_check_at = now() WHERE id = %s",
        (fl.listing_id,),
    )


@pytest.mark.parametrize(
    ("status", "with_version", "with_at"),
    [
        ("clean", True, False),
        ("issues", True, False),
        ("pending", False, False),
        ("done", True, True),
    ],
)
def test_remote_check_refused(db: Conn, status: str, with_version: bool, with_at: bool) -> None:
    fl = flow(db)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE listings SET remote_check_version_id = %s, remote_check_status = %s, "
        "remote_check_at = %s WHERE id = %s",
        (
            fl.v1 if with_version else None,
            status,
            "2026-11-08T00:00:00Z" if with_at else None,
            fl.listing_id,
        ),
        constraint="ck_listings_remote_check",
    )


def test_remote_check_version_without_status_refused(db: Conn) -> None:
    fl = flow(db)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE listings SET remote_check_version_id = %s WHERE id = %s",
        (fl.v1, fl.listing_id),
        constraint="ck_listings_remote_check",
    )


def test_remote_check_other_listings_version_refused(db: Conn) -> None:
    fl = flow(db)
    other = f.listing(db, fl.tenant_id, f.product(db, fl.tenant_id))
    v_other = next_version(db, fl.tenant_id, other, None, "generation", fl.operator)
    expect_sqlstate(
        db,
        "23503",
        "UPDATE listings SET remote_check_version_id = %s, remote_check_status = 'pending' "
        "WHERE id = %s",
        (v_other, fl.listing_id),
        constraint="fk_listings_remote_check_version_id",
    )


def test_remote_check_fk_is_deferrable(db: Conn) -> None:
    row = db.execute(
        "SELECT condeferrable, condeferred, convalidated FROM pg_constraint "
        "WHERE conname = 'fk_listings_remote_check_version_id'"
    ).fetchone()
    assert row == (True, False, True)
