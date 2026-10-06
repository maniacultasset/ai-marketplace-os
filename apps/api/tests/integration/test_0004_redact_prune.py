"""0004/19_redact_prune (DATABASE.md §9 :1830-1849, :1876; §8.1): user erasure and the migrator-only
pruners with their retention floors."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, as_migrator, scalar
from .ops_support import refresh_token, seed_tenant

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

OLD = "2020-01-01T00:00:00Z"


def test_redact_user_clears_identity(db: Conn) -> None:
    tenant = f.tenant(db)
    user = f.member(db, tenant)
    f.insert(db, "tenant_memberships", tenant_id=f.tenant(db), user_id=user, role="operator")
    with as_migrator(db):
        db.execute("SELECT redact_user(%s)", (user,))
    row = db.execute(
        "SELECT email, hashed_password, display_name, erased_at IS NOT NULL FROM users "
        "WHERE id = %s",
        (user,),
    ).fetchone()
    assert row == (f"erased+{user}@invalid", None, None, True)
    active = db.execute(
        "SELECT count(*) FROM tenant_memberships WHERE user_id = %s AND is_active", (user,)
    ).fetchone()
    assert active == (0,)


def test_redact_user_nulls_audit_email_ip(db: Conn) -> None:
    tenant = f.tenant(db)
    user, other = f.member(db, tenant), f.member(db, tenant)
    mine = f.audit(
        db, tenant, actor_type="user", actor_user_id=user, actor_email="a@x.test", ip="10.0.0.3"
    )
    theirs = f.audit(
        db, tenant, actor_type="user", actor_user_id=other, actor_email="b@x.test", ip="10.0.0.4"
    )
    with as_migrator(db):
        db.execute("SELECT redact_user(%s)", (user,))
    rows: dict[Any, Any] = {
        r[0]: r[1:]
        for r in db.execute(
            "SELECT id, actor_email, ip::text FROM audit_log WHERE id IN (%s, %s)", (mine, theirs)
        ).fetchall()
    }
    assert rows == {mine: (None, None), theirs: ("b@x.test", "10.0.0.4/32")}


def test_redact_user_deletes_tokens_invitations(db: Conn) -> None:
    side = seed_tenant(db)
    email = scalar(db, "SELECT email FROM users WHERE id = %s", (side.user_id,))
    refresh_token(db, side)
    other_side = seed_tenant(db)
    kept = refresh_token(db, other_side)
    f.insert(
        db,
        "invitations",
        tenant_id=other_side.tenant_id,
        email=str(email).lower(),
        role="reviewer",
        token_hash=bytes(range(32)),
        expires_at="2099-01-01T00:00:00Z",
    )
    with as_migrator(db):
        db.execute("SELECT redact_user(%s)", (side.user_id,))
    assert db.execute(
        "SELECT count(*) FROM refresh_tokens WHERE user_id = %s", (side.user_id,)
    ).fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM refresh_tokens WHERE id = %s", (kept,)).fetchone() == (
        1,
    )
    assert db.execute(
        "SELECT count(*) FROM invitations WHERE lower(email) = lower(%s)", (email,)
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("call", "inside"),
    [
        ("SELECT prune_audit_log(%s)", "now() - interval '23 months'"),
        ("SELECT prune_append_only('agent_runs', %s)", "now() - interval '12 months'"),
        ("SELECT prune_closed('listing_issues', %s)", "now() - interval '12 months'"),
    ],
)
def test_prune_refuses_inside_floor(db: Conn, call: str, inside: str) -> None:
    cutoff = scalar(db, f"SELECT {inside}")
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", call, (cutoff,))


def test_prune_refuses_other_tables(db: Conn) -> None:
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", "SELECT prune_append_only('review_decisions', %s)", (OLD,))
        expect_sqlstate(db, "GRD01", "SELECT prune_closed('listings', %s)", (OLD,))


def _cutoff(db: Conn, months: int) -> Any:
    return scalar(db, "SELECT now() - make_interval(months => %s)", (months,))


def test_prune_deletes_old_rows(db: Conn) -> None:
    tenant = f.tenant(db)
    old_audit, new_audit = f.audit(db, tenant, occurred_at=OLD), f.audit(db, tenant)
    old_run, new_run = f.agent_run(db, tenant, created_at=OLD), f.agent_run(db, tenant)
    listing = f.listing(db, tenant)
    old_vr = f.validation_run(db, tenant, listing, created_at=OLD)
    with as_migrator(db):
        assert scalar(db, "SELECT prune_audit_log(%s)", (_cutoff(db, 25),)) >= 1
        assert scalar(db, "SELECT prune_append_only('agent_runs', %s)", (_cutoff(db, 14),)) >= 1
        assert (
            scalar(db, "SELECT prune_append_only('validation_runs', %s)", (_cutoff(db, 14),)) >= 1
        )
    gone = {
        str(r[0])
        for r in db.execute(
            "SELECT 'audit' FROM audit_log WHERE id = %s UNION ALL "
            "SELECT 'run' FROM agent_runs WHERE id = %s UNION ALL "
            "SELECT 'vr' FROM validation_runs WHERE id = %s",
            (old_audit, old_run, old_vr),
        ).fetchall()
    }
    assert gone == set()
    kept = db.execute(
        "SELECT (SELECT count(*) FROM audit_log WHERE id = %s), "
        "(SELECT count(*) FROM agent_runs WHERE id = %s)",
        (new_audit, new_run),
    ).fetchone()
    assert kept == (1, 1)


def _closed_issue(db: Conn, tenant: UUID, listing: UUID, status: str) -> UUID:
    columns: dict[str, Any] = {"first_seen_at": OLD, "last_seen_at": OLD}
    if status != "open":
        columns.update(status="resolved", resolution="human_edit", resolved_at=OLD)
    return f.listing_issue(
        db,
        tenant,
        listing,
        error_code="FIELD_TOO_LONG",
        field_path=f"content.{status}",
        **columns,
    )


def test_prune_closed_only_closed(db: Conn) -> None:
    tenant = f.tenant(db)
    listing = f.listing(db, tenant)
    open_issue = _closed_issue(db, tenant, listing, "open")
    closed_issue = _closed_issue(db, tenant, listing, "resolved")
    owner = f.member(db, tenant)
    applied = f.human_task(
        db,
        tenant,
        status="applied",
        answer={"value": "x"},
        answered_by_user_id=owner,
        answered_at=OLD,
        applied_at=OLD,
        updated_at=OLD,
    )
    live_task = f.human_task(db, tenant, target_path="facts.fit", updated_at=OLD)
    with as_migrator(db):
        assert scalar(db, "SELECT prune_closed('listing_issues', %s)", (_cutoff(db, 14),)) >= 1
        assert scalar(db, "SELECT prune_closed('human_tasks', %s)", (_cutoff(db, 14),)) >= 1
    left = db.execute(
        "SELECT id FROM listing_issues WHERE id IN (%s, %s) UNION ALL "
        "SELECT id FROM human_tasks WHERE id IN (%s, %s)",
        (open_issue, closed_issue, applied, live_task),
    ).fetchall()
    assert {r[0] for r in left} == {open_issue, live_task}


@pytest.mark.parametrize(
    "call",
    [
        "SELECT redact_user(%s)",
        "SELECT prune_audit_log(%s::timestamptz)",
        "SELECT prune_append_only('agent_runs', %s::timestamptz)",
        "SELECT prune_closed('human_tasks', %s::timestamptz)",
        "SELECT purge_tenant(%s, 'data')",
    ],
)
def test_migrator_only_functions(db: Conn, call: str) -> None:
    value: Any = OLD if "timestamptz" in call else f.tenant(db)
    with as_app_rw(db):
        expect_sqlstate(db, "42501", call, (value,))
