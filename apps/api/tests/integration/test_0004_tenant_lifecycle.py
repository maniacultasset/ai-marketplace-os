"""0004/16_tenant_lifecycle (DATABASE.md §5.1 tenants :229, §9 :1855-1870): the two SECURITY DEFINER
functions that alone move a tenant's lifecycle columns."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, as_migrator, scalar, set_gucs
from .ops_support import refresh_token, seed_tenant

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

LIFECYCLE = "SELECT set_tenant_lifecycle(%s, %s, %s)"


def _status(db: Conn, tenant: UUID) -> tuple[Any, ...]:
    row = db.execute(
        "SELECT status, offboarded_at IS NOT NULL, purge_after - offboarded_at FROM tenants "
        "WHERE id = %s",
        (tenant,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_lifecycle_other_tenant_refused(db: Conn) -> None:
    mine, other = f.tenant(db), f.tenant(db)
    with as_app_rw(db):
        set_gucs(db, tenant_id=mine)
        expect_sqlstate(db, "GRD01", LIFECYCLE, (other, "offboarded", "x"))
        set_gucs(db, tenant_id=None)
        expect_sqlstate(db, "GRD01", LIFECYCLE, (mine, "offboarded", "x"))


def test_lifecycle_offboard_sets_purge_after(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        set_gucs(db, tenant_id=tenant)
        db.execute(LIFECYCLE, (tenant, "offboarded", "contract ended"))
    status = _status(db, tenant)
    assert status[:2] == ("offboarded", True)
    assert str(status[2]) == "30 days, 0:00:00"
    with as_migrator(db):
        db.execute(LIFECYCLE, (tenant, "active", "reactivated"))
    assert _status(db, tenant) == ("active", False, None)


def test_lifecycle_app_rw_cannot_unsuspend(db: Conn) -> None:
    tenant = f.tenant(db, status="suspended")
    with as_app_rw(db):
        set_gucs(db, tenant_id=tenant)
        expect_sqlstate(db, "GRD01", LIFECYCLE, (tenant, "active", "self-service"))
    active = f.tenant(db)
    with as_app_rw(db):
        set_gucs(db, tenant_id=active)
        expect_sqlstate(db, "GRD01", LIFECYCLE, (active, "suspended", "x"))


def test_lifecycle_suspend_revokes_tokens(db: Conn) -> None:
    side = seed_tenant(db)
    live = refresh_token(db, side)
    revoked = refresh_token(db, side, revoked_at="2026-10-01T00:00:00Z", revoked_reason="logout")
    other = refresh_token(db, seed_tenant(db))
    with as_migrator(db):
        db.execute(LIFECYCLE, (side.tenant_id, "suspended", "non-payment"))
    rows: dict[Any, Any] = dict(
        db.execute(
            "SELECT id, revoked_reason FROM refresh_tokens WHERE id IN (%s, %s, %s)",
            (live, revoked, other),
        ).fetchall()
    )
    assert rows == {live: "tenant_suspended", revoked: "logout", other: None}


def test_lifecycle_audited(db: Conn) -> None:
    tenant = f.tenant(db)
    user = f.member(db, tenant)
    with as_app_rw(db):
        set_gucs(db, tenant_id=tenant, user_id=user)
        db.execute(LIFECYCLE, (tenant, "offboarded", "erasure request"))
    row = db.execute(
        "SELECT actor_type, actor_user_id, action, entity_type, entity_id, reason FROM audit_log "
        "WHERE tenant_id = %s",
        (tenant,),
    ).fetchone()
    assert row == ("user", user, "tenant.offboarded", "tenant", tenant, "erasure request")


@pytest.mark.parametrize(
    ("start", "target"),
    [
        ("active", "trial"),
        ("suspended", "trial"),
        ("active", "active"),
        ("offboarded", "suspended"),
    ],
)
def test_lifecycle_illegal_transition(db: Conn, start: str, target: str) -> None:
    columns: dict[str, Any] = {"status": start}
    if start == "offboarded":
        columns["offboarded_at"] = "2026-10-01T00:00:00Z"
        columns["purge_after"] = "2099-01-01T00:00:00Z"
    tenant = f.tenant(db, **columns)
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", LIFECYCLE, (tenant, target, "x"))


def test_lifecycle_reactivation_after_purge_after_refused(db: Conn) -> None:
    tenant = f.tenant(
        db,
        status="offboarded",
        offboarded_at="2026-01-01T00:00:00Z",
        purge_after="2026-02-01T00:00:00Z",
    )
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", LIFECYCLE, (tenant, "active", "too late"))


def test_expire_trials_only_past_trials(db: Conn) -> None:
    past = f.tenant(db, status="trial", trial_ends_at="2026-01-01T00:00:00Z")
    future = f.tenant(db, status="trial", trial_ends_at="2099-01-01T00:00:00Z")
    active = f.tenant(db)
    with as_app_rw(db):
        expired = scalar(db, "SELECT expire_trials()")
    assert expired >= 1
    statuses: dict[Any, Any] = dict(
        db.execute(
            "SELECT id, status FROM tenants WHERE id IN (%s, %s, %s)", (past, future, active)
        ).fetchall()
    )
    assert statuses == {past: "suspended", future: "trial", active: "active"}
    audit = db.execute(
        "SELECT action, reason FROM audit_log WHERE tenant_id = %s", (past,)
    ).fetchall()
    assert audit == [("tenant.suspended", "trial_expired")]


def test_status_not_directly_updatable(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        expect_sqlstate(
            db, "42501", "UPDATE tenants SET status = 'suspended' WHERE id = %s", (tenant,)
        )


def test_lifecycle_functions_definer_and_granted(db: Conn) -> None:
    rows = db.execute(
        "SELECT p.proname, p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner), "
        "has_function_privilege('app_rw', p.oid, 'EXECUTE'), "
        "has_function_privilege('app_ro', p.oid, 'EXECUTE') "
        "FROM pg_proc p WHERE p.proname IN ('set_tenant_lifecycle', 'expire_trials') ORDER BY 1"
    ).fetchall()
    config = ["search_path=pg_catalog, public, pg_temp"]
    assert rows == [
        ("expire_trials", True, config, "migrator", True, False),
        ("set_tenant_lifecycle", True, config, "migrator", True, False),
    ]
