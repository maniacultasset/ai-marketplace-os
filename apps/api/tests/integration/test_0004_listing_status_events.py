"""0004/06b_status_events (DATABASE.md §6.1 :1402 event rules; §5.3 :805 decision order, :948
publish statement order): every seeded edge passes with its prerequisites, and each event rule
refuses a transition that lacks its evidence."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from .conftest import expect_sqlstate
from .integrity_support import (
    approve_current,
    approved_flow,
    decision,
    flow,
    force_state,
    next_version,
    prepare_edge,
    queued_job,
    set_gucs,
    submission,
    transition_sql,
)
from .test_0004_transitions import SEED

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

EDGE_SQL = (
    "SELECT from_status, to_status, event, actor, requires_current_approval, voids_approval, "
    "allowed_reasons, requires_from_reason FROM listing_status_transitions "
    "WHERE from_status = %s AND to_status = %s AND event = %s"
)


@pytest.mark.parametrize("key", [s[:3] for s in SEED], ids=lambda k: "-".join(k))
def test_seed_edges_pass(db: Conn, key: tuple[str, str, str]) -> None:
    edge = db.execute(EDGE_SQL, key).fetchone()
    assert edge is not None
    _, query, params = prepare_edge(db, edge)
    db.execute(query, params)
    row = db.execute("SELECT status, last_event FROM listings WHERE id = %s", (params[-1],))
    assert row.fetchone() == (key[1], key[2])


def _validated(db: Conn) -> Any:
    fl = flow(db)
    force_state(db, fl.listing_id, "validated")
    set_gucs(db, user_id=fl.reviewer)
    return fl


def test_approve_needs_decision_this_tx(db: Conn) -> None:
    fl = _validated(db)
    approved_flow(db, fl)
    query, params = transition_sql(fl.listing_id, "approved", "approve")
    set_gucs(db, user_id=fl.owner)  # the decision is not by the transaction's user
    expect_sqlstate(db, "LST01", query, params)
    set_gucs(db, user_id=fl.reviewer)
    db.execute(query, params)


def test_approve_decision_from_earlier_transaction_fails(db: Conn) -> None:
    fl = _validated(db)
    decision(db, fl, "approve", fl.v1, fl.reviewer, decided_at="2026-10-01T00:00:00Z")
    approve_current(db, fl)
    query, params = transition_sql(fl.listing_id, "approved", "approve")
    expect_sqlstate(db, "LST01", query, params)


def test_regenerate_needs_regenerate_requested(db: Conn) -> None:
    fl = _validated(db)
    job = queued_job(db, fl, "listing_generate")
    decision(db, fl, "reject", fl.v1, fl.reviewer)
    query, params = transition_sql(fl.listing_id, "generating", "regenerate")
    expect_sqlstate(db, "LST01", query, params)
    decision(db, fl, "regenerate_requested", fl.v1, fl.reviewer, regeneration_job_id=job)
    db.execute(query, params)


def test_generate_needs_queued_job(db: Conn) -> None:
    fl = flow(db)
    set_gucs(db, user_id=fl.operator)
    query, params = transition_sql(fl.listing_id, "generating", "generate")
    expect_sqlstate(db, "LST01", query, params)
    job = queued_job(db, fl, "listing_generate")
    db.execute(
        "UPDATE job_runs SET status = 'cancelled', finished_at = now() WHERE id = %s", (job,)
    )
    expect_sqlstate(db, "LST01", query, params)
    queued_job(db, fl, "listing_generate")
    db.execute(query, params)


def _approved(db: Conn) -> Any:
    fl = flow(db)
    set_gucs(db, user_id=fl.operator)
    approved_flow(db, fl)
    force_state(db, fl.listing_id, "approved")
    return fl


def test_publish_needs_queued_publish_job(db: Conn) -> None:
    fl = _approved(db)
    query, params = transition_sql(fl.listing_id, "publishing", "publish")
    queued_job(db, fl, "listing_generate")
    expect_sqlstate(db, "LST01", query, params)
    queued_job(db, fl, "listing_publish")
    db.execute(query, params)


def test_publish_statement_order_wrong_fails(db: Conn) -> None:
    """UPDATE listings before INSERT job_runs fails; the documented order passes."""
    fl = _approved(db)
    query, params = transition_sql(fl.listing_id, "publishing", "publish")
    expect_sqlstate(db, "LST01", query, params)
    queued_job(db, fl, "listing_publish")
    db.execute(query, params)


def test_publish_after_three_submissions_refused(db: Conn) -> None:
    fl = _approved(db)
    job = queued_job(db, fl, "listing_publish")
    previous = None
    for attempt in (1, 2, 3):
        # set-up rows only: the submission guard (ls_guard) is not under test here
        db.execute("SET LOCAL session_replication_role = replica")
        previous = submission(
            db,
            fl,
            job,
            attempt=attempt,
            outcome="error",
            error_code="RATE_LIMITED",
            error_report_json={"errors": []},
            responded_at="2026-10-01T00:00:00Z",
            resolved_at="2026-10-01T00:00:00Z",
            retry_justification=None if attempt == 1 else "retryable_condition",
            previous_submission_id=previous,
        )
        db.execute("SET LOCAL session_replication_role = origin")
    query, params = transition_sql(fl.listing_id, "publishing", "publish")
    expect_sqlstate(db, "LST01", query, params)


def test_status_requires_current_approval(db: Conn) -> None:
    """The publish job passed the gate, then a new current version made the approval stale."""
    fl = flow(db)
    set_gucs(db, user_id=fl.operator)
    approved_flow(db, fl)
    force_state(db, fl.listing_id, "failed", "publish_failed")
    queued_job(db, fl, "listing_publish")
    next_version(db, fl.tenant_id, fl.listing_id, fl.v1, "regeneration", fl.operator)
    query, params = transition_sql(fl.listing_id, "publishing", "retry_publish")
    expect_sqlstate(db, "LST01", query, params)


def test_status_voids_approval(db: Conn) -> None:
    fl = _approved(db)
    job = queued_job(db, fl, "listing_generate")
    decision(db, fl, "regenerate_requested", fl.v1, fl.operator, regeneration_job_id=job)
    query, params = transition_sql(fl.listing_id, "generating", "regenerate")
    expect_sqlstate(db, "LST01", query, params)
    query, params = transition_sql(
        fl.listing_id, "generating", "regenerate", approved_version_id=None
    )
    db.execute(query, params)


def test_edit_needs_new_edit_version(db: Conn) -> None:
    fl = _validated(db)
    query, params = transition_sql(fl.listing_id, "needs_review", "edit")
    expect_sqlstate(db, "LST01", query, params)
    regen = next_version(
        db, fl.tenant_id, fl.listing_id, fl.v1, "regeneration", fl.operator, current=False
    )
    query, params = transition_sql(fl.listing_id, "needs_review", "edit", current_version_id=regen)
    expect_sqlstate(db, "LST01", query, params)
    edit = next_version(
        db,
        fl.tenant_id,
        fl.listing_id,
        regen,
        "edit",
        fl.operator,
        edited_by=fl.operator,
        current=False,
    )
    query, params = transition_sql(fl.listing_id, "needs_review", "edit", current_version_id=edit)
    db.execute(query, params)


def test_submission_accepted_sets_published_version(db: Conn) -> None:
    fl = _approved(db)
    job = queued_job(db, fl, "listing_publish")
    db.execute(*transition_sql(fl.listing_id, "publishing", "publish"))
    sub = submission(db, fl, job)
    set_gucs(db, job_id=job)
    columns = {
        "published_version_id": fl.v1,
        "first_published_at": "2026-10-06T00:00:00Z",
        "marketplace_state": "processing",
    }
    query, params = transition_sql(fl.listing_id, "published", "submission_accepted", **columns)
    expect_sqlstate(db, "LST01", query, params)  # the submission is still pending
    db.execute(
        "UPDATE listing_submissions SET outcome = 'accepted', responded_at = now() WHERE id = %s",
        (sub,),
    )
    db.execute(query, params)
