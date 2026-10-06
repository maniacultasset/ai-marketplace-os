"""0004/06_listings_status_guard (DATABASE.md §5.3 listings :767, §6.1 :1402): the table-driven
core of the listings status guard."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import (
    approve_current,
    decision,
    flow,
    force_state,
    queued_job,
    set_gucs,
    transition_sql,
)

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

# Every (from, to, event) that is not a seeded edge raises LST01. The loop runs in the database:
# for each candidate it puts the listing back in `from` (user triggers skipped for that set-up
# statement) and attempts the guarded UPDATE, collecting every outcome that is not LST01. A
# from = to candidate is a transition only when the reason changes (blocked, failed).
EXHAUSTIVE = """
CREATE FUNCTION pg_temp.unseeded_failures(p_listing uuid) RETURNS TABLE (f text, t text, e text, got text)
LANGUAGE plpgsql AS $$
DECLARE
  st text[] := ARRAY['created','generating','validated','needs_review','approved','publishing',
                     'published','blocked','failed'];
  ev text[];
  r_from text; r_to text; fs text; ts text; e2 text;
BEGIN
  SELECT array_agg(DISTINCT x) INTO ev FROM unnest(ARRAY[
    'generate','generation_clean','generation_warnings','corrections_exhausted',
    'human_task_opened','generation_failed','approve','reject','reject_regenerate','regenerate',
    'edit','revalidate','task_answered','publish','submission_accepted','submission_failed',
    'submission_attempts_exhausted','marketplace_action_required','marketplace_rejected',
    'sync_exhausted','retry_publish','auto_resubmit','resume_publish','create','sync',
    'refresh_requested','pause','unpause','archive','unarchive']) AS x;
  FOREACH fs IN ARRAY st LOOP
    FOREACH ts IN ARRAY st LOOP
      CONTINUE WHEN fs = ts AND fs NOT IN ('blocked', 'failed');
      FOREACH e2 IN ARRAY ev LOOP
        CONTINUE WHEN EXISTS (SELECT 1 FROM listing_status_transitions x
                               WHERE x.from_status = fs AND x.to_status = ts AND x.event = e2);
        r_from := CASE fs WHEN 'blocked' THEN 'validation_errors' WHEN 'failed' THEN 'publish_failed' END;
        r_to := CASE ts WHEN 'blocked' THEN CASE WHEN fs = 'blocked' THEN 'rejected' ELSE 'validation_errors' END
                        WHEN 'failed' THEN CASE WHEN fs = 'failed' THEN 'generation_failed' ELSE 'publish_failed' END END;
        SET LOCAL session_replication_role = replica;
        UPDATE listings SET status = fs, status_reason = r_from WHERE id = p_listing;
        SET LOCAL session_replication_role = origin;
        BEGIN
          UPDATE listings SET status = ts, status_reason = r_to, last_event = e2 WHERE id = p_listing;
          f := fs; t := ts; e := e2; got := 'succeeded'; RETURN NEXT;
        EXCEPTION WHEN OTHERS THEN
          IF SQLSTATE <> 'LST01' THEN f := fs; t := ts; e := e2; got := SQLSTATE; RETURN NEXT; END IF;
        END;
      END LOOP;
    END LOOP;
  END LOOP;
END $$;
"""


def test_transition_table_exhaustive(db: Conn) -> None:
    fl = flow(db)
    approve_current(db, fl)
    force_state(
        db,
        fl.listing_id,
        "validated",
        published_version_id=fl.v1,
        first_published_at="2026-10-01T00:00:00Z",
        marketplace_state="processing",
    )
    set_gucs(db, user_id=fl.operator, job_id=uuid4())
    db.execute(EXHAUSTIVE)
    failures = db.execute(
        "SELECT * FROM pg_temp.unseeded_failures(%s)", (fl.listing_id,)
    ).fetchall()
    assert failures == []


def _seeded_ok(db: Conn) -> tuple[Any, ...]:
    """A validated listing and the args of a seeded `revalidate` edge into needs_review."""
    fl = flow(db)
    force_state(db, fl.listing_id, "validated")
    set_gucs(db, user_id=fl.operator)
    return fl, transition_sql(fl.listing_id, "needs_review", "revalidate")


def test_status_paused_freezes(db: Conn) -> None:
    fl, (query, params) = _seeded_ok(db)
    force_state(db, fl.listing_id, "validated", paused_at="2026-10-01T00:00:00Z")
    error = expect_sqlstate(db, "LST02", query, params)
    assert error.diag.constraint_name == "paused"


def test_status_archived_freezes(db: Conn) -> None:
    fl, (query, params) = _seeded_ok(db)
    force_state(db, fl.listing_id, "validated", archived_at="2026-10-01T00:00:00Z")
    error = expect_sqlstate(db, "LST02", query, params)
    assert error.diag.constraint_name == "archived"


def test_status_seeded_edge_passes(db: Conn) -> None:
    _, (query, params) = _seeded_ok(db)
    db.execute(query, params)


def test_status_user_actor_needs_user_guc(db: Conn) -> None:
    fl = flow(db)
    queued_job(db, fl, "listing_generate")
    query, params = transition_sql(fl.listing_id, "generating", "generate")
    expect_sqlstate(db, "LST01", query, params)
    set_gucs(db, user_id=fl.operator)
    db.execute(query, params)


def test_status_worker_actor_needs_job_guc(db: Conn) -> None:
    fl = flow(db)
    force_state(db, fl.listing_id, "generating")
    query, params = transition_sql(fl.listing_id, "validated", "generation_clean")
    set_gucs(db, user_id=fl.operator)
    expect_sqlstate(db, "LST01", query, params)
    set_gucs(db, job_id=uuid4())
    db.execute(query, params)


def test_status_reason_not_allowed(db: Conn) -> None:
    fl = flow(db)
    force_state(db, fl.listing_id, "generating")
    set_gucs(db, job_id=uuid4())
    query, params = transition_sql(fl.listing_id, "blocked", "corrections_exhausted", "rejected")
    expect_sqlstate(db, "LST01", query, params)
    query, params = transition_sql(
        fl.listing_id, "blocked", "corrections_exhausted", "corrections_exhausted"
    )
    db.execute(query, params)


def test_status_requires_from_reason(db: Conn) -> None:
    fl = flow(db)
    set_gucs(db, user_id=fl.operator)
    query, params = transition_sql(fl.listing_id, "validated", "task_answered")
    force_state(db, fl.listing_id, "blocked", "rejected")
    expect_sqlstate(db, "LST01", query, params)
    force_state(db, fl.listing_id, "blocked", "human_task_open")
    db.execute(query, params)


def test_status_stamps_timestamps(db: Conn) -> None:
    fl, (query, params) = _seeded_ok(db)
    force_state(
        db,
        fl.listing_id,
        "validated",
        status_changed_at="2026-01-01T00:00:00Z",
        last_event_at="2026-01-01T00:00:00Z",
    )
    db.execute(query, params)
    row = db.execute(
        "SELECT status_changed_at = now(), last_event_at = now(), last_event FROM listings "
        "WHERE id = %s",
        (fl.listing_id,),
    ).fetchone()
    assert row == (True, True, "revalidate")


def test_status_external_id_needs_job(db: Conn) -> None:
    fl = flow(db)
    db.execute(
        "UPDATE listings SET external_id = 'B0FIRST', marketplace_sku = 'SKU-A' WHERE id = %s",
        (fl.listing_id,),
    )
    for column in ("external_id", "marketplace_sku"):
        expect_sqlstate(
            db,
            "GRD01",
            f"UPDATE listings SET {column} = 'CHANGED' WHERE id = %s",  # noqa: S608
            (fl.listing_id,),
        )
    set_gucs(db, job_id=uuid4())
    db.execute("UPDATE listings SET external_id = 'B0SECOND' WHERE id = %s", (fl.listing_id,))


def test_status_variant_child_refused(db: Conn) -> None:
    tenant = f.tenant(db)
    style = f.product(db, tenant, "style")
    child = f.product(db, tenant, "child", parent=style)
    expect_sqlstate(
        db,
        "GRD01",
        "INSERT INTO listings (tenant_id, product_id, marketplace, category) "
        "VALUES (%s, %s, 'amazon', 't_shirts')",
        (tenant, child),
    )
    f.listing(db, tenant, style)


def test_status_suspended_tenant_blocks_approved(db: Conn) -> None:
    fl = flow(db, status="suspended")
    force_state(db, fl.listing_id, "validated")
    set_gucs(db, user_id=fl.reviewer)
    decision(db, fl, "approve", fl.v1, fl.reviewer)
    approve_current(db, fl)
    query, params = transition_sql(fl.listing_id, "approved", "approve")
    expect_sqlstate(db, "LST01", query, params)
    db.execute("UPDATE tenants SET status = 'active' WHERE id = %s", (fl.tenant_id,))
    db.execute(query, params)
