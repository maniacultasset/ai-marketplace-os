"""S10a tester attacks on 0004 (independent of the implementer's suites): GUC forgery by `app_rw`,
cross-tenant purge GUCs, multi-tenant approvers and approval timestamps. Raw SQL on the rolled-back
`db` transaction."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import APPROVE_SQL, as_app_rw, as_migrator, next_version, scalar, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

APPEND_ONLY = ("audit_log", "agent_runs", "validation_runs", "review_decisions")


def _closed_issue(db: Conn, tenant: UUID) -> UUID:
    issue = f.listing_issue(db, tenant, f.listing(db, tenant))
    db.execute(
        "UPDATE listing_issues SET status = 'resolved', resolution = 'human_edit', "
        "resolved_at = now() WHERE id = %s",
        (issue,),
    )
    return issue


def _terminal_task(db: Conn, tenant: UUID) -> UUID:
    task = f.human_task(db, tenant)
    db.execute(
        "UPDATE human_tasks SET status = 'cancelled', cancelled_reason = 'superseded' WHERE id = %s",
        (task,),
    )
    return task


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_app_rw_forged_gucs_still_refused(db: Conn, table: str) -> None:
    """`app_rw` can set every app.* GUC itself; the session user is never `migrator`, and the
    privilege is gone anyway."""
    tenant = f.tenant(db)
    user = f.member(db, tenant)
    f.audit(db, tenant, actor_type="user", actor_user_id=user)
    f.agent_run(db, tenant)
    listing = f.listing(db, tenant)
    f.validation_run(db, tenant, listing)
    f.review_decision(db, tenant, listing, f.version(db, tenant, listing))
    with as_app_rw(db):
        set_gucs(db, purge_tenant=tenant, prune_table=table, redact_user=user, tenant_id=tenant)
        for statement in (
            "DELETE FROM {} WHERE tenant_id = %s",
            "UPDATE {} SET id = id WHERE tenant_id = %s",
        ):
            expect_sqlstate(
                db, "42501", sql.SQL(statement).format(sql.Identifier(table)), (tenant,)
            )


@pytest.mark.parametrize("table", ["listing_issues", "human_tasks"])
def test_app_rw_forged_gucs_cannot_delete_closed_rows(db: Conn, table: str) -> None:
    tenant = f.tenant(db)
    row = _closed_issue(db, tenant) if table == "listing_issues" else _terminal_task(db, tenant)
    with as_app_rw(db):
        set_gucs(db, purge_tenant=tenant, prune_table=table)
        delete = sql.SQL("DELETE FROM {} WHERE id = %s").format(sql.Identifier(table))
        expect_sqlstate(db, "42501", delete, (row,))


def test_migrator_purge_of_a_cannot_touch_b_append_only_rows(db: Conn) -> None:
    acme, bharat = f.tenant(db), f.tenant(db)
    f.audit(db, acme)
    theirs = f.audit(db, bharat)
    with as_migrator(db):
        set_gucs(db, purge_tenant=acme)
        db.execute("DELETE FROM audit_log WHERE tenant_id = %s", (acme,))
        expect_sqlstate(db, "GRD01", "DELETE FROM audit_log WHERE id = %s", (theirs,))
        expect_sqlstate(db, "GRD01", "UPDATE audit_log SET ip = NULL WHERE id = %s", (theirs,))


def test_lv_guard_reads_the_versions_tenant_membership(db: Conn) -> None:
    """A user who reviews in tenant B but only operates in tenant A cannot approve A's version."""
    acme, bharat = f.tenant(db), f.tenant(db)
    listing = f.listing(db, acme)
    version = next_version(
        db, acme, listing, None, "generation", f.member(db, acme, role="operator")
    )
    dual = f.user(db)
    f.insert(db, "tenant_memberships", tenant_id=acme, user_id=dual, role="operator")
    f.insert(db, "tenant_memberships", tenant_id=bharat, user_id=dual, role="reviewer")
    set_gucs(db, tenant_id=bharat)
    # the superuser bypasses RLS: the guard alone, under a forged GUC naming B
    expect_sqlstate(db, "MKC01", APPROVE_SQL, (dual, version))
    with as_app_rw(db):
        # as app_rw, 0005's RLS hides A's version under B's GUC: nothing to approve
        assert db.execute(APPROVE_SQL, (dual, version)).rowcount == 0
    with as_app_rw(db, acme):
        expect_sqlstate(db, "MKC01", APPROVE_SQL, (dual, version))


def test_lv_guard_stamps_approved_at_now(db: Conn) -> None:
    """A writer cannot back-date the approval: the guard overwrites approved_at with now()."""
    tenant = f.tenant(db)
    listing = f.listing(db, tenant)
    version = next_version(
        db, tenant, listing, None, "generation", f.member(db, tenant, role="operator")
    )
    reviewer = f.member(db, tenant, role="reviewer")
    with as_app_rw(db, tenant):
        db.execute(
            "UPDATE listing_versions SET approved_by_user_id = %s, "
            "approved_at = '2001-01-01T00:00:00Z' WHERE id = %s",
            (reviewer, version),
        )
    stamped = scalar(db, "SELECT approved_at FROM listing_versions WHERE id = %s", (version,))
    assert isinstance(stamped, datetime)
    assert stamped == scalar(db, "SELECT now()")


def test_lv_guard_unicode_content_change_refused(db: Conn) -> None:
    """Content stays immutable for any value, including an approval smuggled in with it."""
    tenant = f.tenant(db)
    listing = f.listing(db, tenant)
    version = next_version(
        db, tenant, listing, None, "generation", f.member(db, tenant, role="operator")
    )
    reviewer = f.member(db, tenant, role="reviewer")
    with as_migrator(db):
        expect_sqlstate(
            db,
            "GRD01",
            "UPDATE listing_versions SET approved_by_user_id = %s, "
            "content_json = jsonb_build_object('title', %s::text) WHERE id = %s",
            (reviewer, "नमस्ते \U0001f600" * 500, version),
        )
