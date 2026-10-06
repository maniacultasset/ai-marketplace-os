"""0004/09_publish_gate (DATABASE.md §5.4 job_runs :947): the BEFORE INSERT gate on a
`listing_publish` job."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import (
    Flow,
    approve_current,
    approved_flow,
    decision,
    flow,
    force_state,
    next_version,
    queued_job,
    set_gucs,
    submission,
)

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

INSERT_PUBLISH = (
    "INSERT INTO job_runs (tenant_id, kind, listing_id, entity_type, entity_id, payload_json, "
    "correlation_id, created_by) VALUES (%s, 'listing_publish', %s, 'listing', %s, '{}', %s, %s)"
)


def _ready(db: Conn, status: str = "approved", reason: str | None = None) -> Flow:
    fl = flow(db)
    set_gucs(db, user_id=fl.operator)
    approved_flow(db, fl)
    force_state(db, fl.listing_id, status, reason)
    return fl


def _params(fl: Flow, created_by: UUID | None = None) -> tuple[Any, ...]:
    by = fl.operator if created_by is None else created_by
    return (fl.tenant_id, fl.listing_id, fl.listing_id, uuid4(), by)


def _refused(db: Conn, fl: Flow, **kw: Any) -> None:
    expect_sqlstate(db, "GRD01", INSERT_PUBLISH, _params(fl, **kw))


def test_publish_gate_approved_current_passes(db: Conn) -> None:
    fl = _ready(db)
    db.execute(INSERT_PUBLISH, _params(fl))
    db.execute(INSERT_PUBLISH.replace("listing_publish", "listing_status_sync"), _params(fl))


def test_publish_gate_owner_passes(db: Conn) -> None:
    fl = _ready(db)
    db.execute(INSERT_PUBLISH, _params(fl, created_by=fl.owner))


def test_publish_gate_not_current(db: Conn) -> None:
    fl = _ready(db)
    next_version(db, fl.tenant_id, fl.listing_id, fl.v1, "regeneration", fl.operator)
    _refused(db, fl)


def test_publish_gate_not_approved(db: Conn) -> None:
    fl = flow(db)
    force_state(db, fl.listing_id, "approved")
    _refused(db, fl)


def test_publish_gate_status_created(db: Conn) -> None:
    fl = _ready(db, "created")
    _refused(db, fl)


@pytest.mark.parametrize("status", ["validated", "needs_review", "publishing", "generating"])
def test_publish_gate_status_refused(db: Conn, status: str) -> None:
    fl = _ready(db, status)
    _refused(db, fl)


def test_publish_gate_failed_retry_passes(db: Conn) -> None:
    fl = _ready(db, "failed", "publish_failed")
    db.execute(INSERT_PUBLISH, _params(fl))


def test_publish_gate_blocked_action_required_passes(db: Conn) -> None:
    fl = _ready(db, "blocked", "marketplace_action_required")
    db.execute(INSERT_PUBLISH, _params(fl))


def test_publish_gate_blocked_other_reason(db: Conn) -> None:
    fl = _ready(db, "blocked", "validation_errors")
    _refused(db, fl)


def test_publish_gate_paused(db: Conn) -> None:
    fl = _ready(db)
    force_state(db, fl.listing_id, "approved", paused_at="2026-10-01T00:00:00Z")
    _refused(db, fl)


def test_publish_gate_three_submissions(db: Conn) -> None:
    fl = _ready(db, "failed", "publish_failed")
    job = queued_job(db, fl, "listing_status_sync")
    previous = None
    for attempt in (1, 2, 3):
        db.execute("SET LOCAL session_replication_role = replica")  # set-up rows only
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
    _refused(db, fl)


@pytest.mark.parametrize("status", ["needs_reconnect", "revoked"])
def test_publish_gate_account_inactive(db: Conn, status: str) -> None:
    fl = _ready(db)
    extra = (
        ", token_enc = NULL, kms_key_version = NULL, revoked_at = now()"
        if status == "revoked"
        else ""
    )
    if status == "needs_reconnect":
        extra = ", needs_reconnect_since = now()"
    db.execute(
        f"UPDATE marketplace_accounts SET status = %s{extra} WHERE id = %s",  # noqa: S608
        (status, fl.account_id),
    )
    _refused(db, fl)


def test_publish_gate_no_account_yet_uses_tenant_account(db: Conn) -> None:
    fl = _ready(db)
    force_state(db, fl.listing_id, "approved", marketplace_account_id=None)
    db.execute(INSERT_PUBLISH, _params(fl))


def _issue(db: Conn, fl: Flow, version: UUID, **columns: Any) -> UUID:
    return f.listing_issue(
        db,
        fl.tenant_id,
        fl.listing_id,
        listing_version_id=version,
        retry_class="user_action",
        error_class="missing_data",
        error_code="MISSING_ATTRIBUTE",
        **columns,
    )


def test_publish_gate_open_user_action_issue(db: Conn) -> None:
    fl = _ready(db)
    _issue(db, fl, fl.v1)
    _refused(db, fl)


def test_publish_gate_warning_issue_ignored(db: Conn) -> None:
    fl = _ready(db)
    _issue(db, fl, fl.v1, severity="warning")
    db.execute(INSERT_PUBLISH, _params(fl))


def test_publish_gate_older_version_issue_ignored(db: Conn) -> None:
    fl = flow(db)
    _issue(db, fl, fl.v1)
    v2 = next_version(db, fl.tenant_id, fl.listing_id, fl.v1, "regeneration", fl.operator)
    set_gucs(db, user_id=fl.operator)
    decision(db, fl, "approve", v2, fl.reviewer)
    approve_current(db, fl, v2)
    force_state(db, fl.listing_id, "approved")
    db.execute(INSERT_PUBLISH, _params(fl))


def test_publish_gate_answered_task_ignored(db: Conn) -> None:
    fl = _ready(db)
    task = f.human_task(
        db,
        fl.tenant_id,
        product_id=fl.product_id,
        status="answered",
        answer={"value": "cotton"},
        answered_by_user_id=fl.owner,
        answered_at="2026-10-01T00:00:00Z",
    )
    _issue(db, fl, fl.v1, human_task_id=task)
    db.execute(INSERT_PUBLISH, _params(fl))


def test_publish_gate_created_by_reviewer(db: Conn) -> None:
    fl = _ready(db)
    _refused(db, fl, created_by=fl.reviewer)


def test_publish_gate_created_by_inactive_operator(db: Conn) -> None:
    fl = _ready(db)
    db.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
        "WHERE tenant_id = %s AND user_id = %s",
        (fl.tenant_id, fl.operator),
    )
    _refused(db, fl)


def test_publish_gate_created_by_null(db: Conn) -> None:
    fl = _ready(db)
    expect_sqlstate(
        db,
        "23514",
        INSERT_PUBLISH.replace("%s)", "NULL)"),
        _params(fl)[:4],
        constraint="ck_job_runs_no_auto_publish",
    )
