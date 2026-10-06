"""0004/12_issues_tasks_guards (DATABASE.md §5.7 :1326-1328 listing_issues_guard, :1355
human_tasks_guard; §9 pruning of closed issues and terminal tasks)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, as_migrator, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

RESOLVE = (
    "UPDATE listing_issues SET status = 'resolved', resolution = 'human_edit', "
    "resolved_at = now() WHERE id = %s"
)
DISMISS = (
    "UPDATE listing_issues SET status = 'dismissed', resolution = 'accepted_warning', "
    "resolved_at = now(), resolved_by_user_id = %s WHERE id = %s"
)


def _issue(db: Conn, **columns: Any) -> tuple[UUID, UUID]:
    tenant = f.tenant(db)
    return tenant, f.listing_issue(db, tenant, f.listing(db, tenant), **columns)


def test_issue_open_to_resolved_ok(db: Conn) -> None:
    _, issue = _issue(db)
    db.execute(
        "UPDATE listing_issues SET occurrences = occurrences + 1, last_seen_at = now(), "
        "message = 'Still too long.' WHERE id = %s",
        (issue,),
    )
    db.execute(RESOLVE, (issue,))


def test_issue_closed_immutable(db: Conn) -> None:
    _, issue = _issue(db)
    db.execute(RESOLVE, (issue,))
    expect_sqlstate(
        db,
        "GRD01",
        "UPDATE listing_issues SET status = 'open', resolution = NULL, "
        "resolved_at = NULL WHERE id = %s",
        (issue,),
    )
    expect_sqlstate(db, "GRD01", "UPDATE listing_issues SET message = 'x' WHERE id = %s", (issue,))


def test_issue_identity_immutable(db: Conn) -> None:
    _, issue = _issue(db)
    for assignment in (
        "error_code = 'BANNED_TERM'",
        "field_path = 'content.bullets'",
        "dedupe_key = 'rules_engine|x|y|'",
    ):
        expect_sqlstate(
            db,
            "GRD01",
            f"UPDATE listing_issues SET {assignment} WHERE id = %s",  # noqa: S608
            (issue,),
        )


def test_issue_error_never_dismissed(db: Conn) -> None:
    tenant, issue = _issue(db)
    reviewer = f.member(db, tenant, role="reviewer")
    expect_sqlstate(db, "GRD01", DISMISS, (reviewer, issue))


def test_issue_dismiss_needs_reviewer_or_owner(db: Conn) -> None:
    tenant, issue = _issue(db, severity="warning")
    for role in ("operator", "admin"):
        expect_sqlstate(db, "GRD01", DISMISS, (f.member(db, tenant, role=role), issue))
    inactive = f.member(db, tenant, role="reviewer", is_active=False)
    expect_sqlstate(db, "GRD01", DISMISS, (inactive, issue))
    db.execute(DISMISS, (f.member(db, tenant, role="owner"), issue))


def test_issue_rule_other_tenant(db: Conn) -> None:
    tenant = f.tenant(db)
    foreign_rule = f.rule(db, f.tenant(db))
    expect_sqlstate(
        db,
        "GRD01",
        "INSERT INTO listing_issues (tenant_id, listing_id, marketplace, source, field_path, "
        "error_code, error_class, retry_class, severity, message, dedupe_key, rule_id) "
        "VALUES (%s, %s, 'amazon', 'rules_engine', 'content.title', 'BANNED_TERM', "
        "'content_error', 'ai_correct', 'error', 'm', %s, %s)",
        (
            tenant,
            f.listing(db, tenant),
            f"rules_engine|content.title|BANNED_TERM|{foreign_rule}",
            foreign_rule,
        ),
    )
    own = f.rule(db, tenant)
    f.listing_issue(db, tenant, f.listing(db, tenant), rule_id=own, error_code="BANNED_TERM")


def test_issue_platform_rule_ok(db: Conn) -> None:
    tenant = f.tenant(db)
    platform = f.rule(db, None)
    f.listing_issue(db, tenant, f.listing(db, tenant), rule_id=platform)


def test_issue_no_delete(db: Conn) -> None:
    _, issue = _issue(db)
    with as_app_rw(db):
        expect_sqlstate(db, "42501", "DELETE FROM listing_issues WHERE id = %s", (issue,))
    db.execute(RESOLVE, (issue,))
    expect_sqlstate(db, "GRD01", "DELETE FROM listing_issues WHERE id = %s", (issue,))
    with as_migrator(db):
        set_gucs(db, prune_table="listing_issues")
        db.execute("DELETE FROM listing_issues WHERE id = %s", (issue,))


# ---- human_tasks ----

ANSWER = (
    "UPDATE human_tasks SET status = 'answered', answer = '{\"value\": \"cotton\"}', "
    "answered_by_user_id = %s, answered_at = now() WHERE id = %s"
)
APPLY = "UPDATE human_tasks SET status = 'applied', applied_at = now() WHERE id = %s"


def _task(db: Conn) -> tuple[UUID, UUID, UUID]:
    tenant = f.tenant(db)
    owner = f.member(db, tenant)
    return tenant, owner, f.human_task(db, tenant)


def test_task_open_answered_applied_ok(db: Conn) -> None:
    _, owner, task = _task(db)
    db.execute(ANSWER, (owner, task))
    db.execute(APPLY, (task,))


def test_task_open_cancelled_ok(db: Conn) -> None:
    _, _, task = _task(db)
    db.execute(
        "UPDATE human_tasks SET status = 'cancelled', cancelled_reason = 'superseded' WHERE id = %s",
        (task,),
    )


def test_task_applied_to_open_refused(db: Conn) -> None:
    _, owner, task = _task(db)
    expect_sqlstate(db, "GRD01", APPLY, (task,))  # open → applied skips answered
    db.execute(ANSWER, (owner, task))
    db.execute(APPLY, (task,))
    expect_sqlstate(
        db,
        "GRD01",
        "UPDATE human_tasks SET status = 'open', answer = NULL, answered_by_user_id = NULL, "
        "answered_at = NULL, applied_at = NULL WHERE id = %s",
        (task,),
    )


def test_task_terminal_immutable(db: Conn) -> None:
    _, owner, task = _task(db)
    db.execute(ANSWER, (owner, task))
    db.execute(APPLY, (task,))
    expect_sqlstate(db, "GRD01", "UPDATE human_tasks SET request_id = 'x' WHERE id = %s", (task,))


def test_task_answer_set_once(db: Conn) -> None:
    _, owner, task = _task(db)
    db.execute(ANSWER, (owner, task))
    expect_sqlstate(
        db, "GRD01", 'UPDATE human_tasks SET answer = \'{"value": "silk"}\' WHERE id = %s', (task,)
    )


@pytest.mark.parametrize(
    "assignment",
    [
        "kind = 'conflicting_facts'",
        "question = 'Another question?'",
        "target_path = 'facts.fit'",
        'expected_answer = \'{"kind": "number"}\'',
    ],
)
def test_task_kind_immutable(db: Conn, assignment: str) -> None:
    _, _, task = _task(db)
    expect_sqlstate(
        db, "GRD01", f"UPDATE human_tasks SET {assignment} WHERE id = %s", (task,)  # noqa: S608
    )


def test_task_no_delete(db: Conn) -> None:
    tenant, owner, task = _task(db)
    with as_app_rw(db):
        expect_sqlstate(db, "42501", "DELETE FROM human_tasks WHERE id = %s", (task,))
    db.execute(ANSWER, (owner, task))
    db.execute(APPLY, (task,))
    expect_sqlstate(db, "GRD01", "DELETE FROM human_tasks WHERE id = %s", (task,))
    with as_migrator(db):
        set_gucs(db, purge_tenant=tenant)
        db.execute("DELETE FROM human_tasks WHERE id = %s", (task,))
