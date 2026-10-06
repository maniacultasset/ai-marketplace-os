"""0004/18_purge_tenant (DATABASE.md §9 :1871-1874; §8.1): the two-phase tenant purge, migrator only,
over a tenant holding a row in every tenant table and one listing driven to `published` through
the guards; the other tenant is untouched."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import (
    approved_flow,
    as_app_rw,
    as_migrator,
    flow,
    force_state,
    queued_job,
    set_gucs,
    submission,
    transition,
)
from .test_0003_attacks import _full_tenant

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

# The mechanics suites purge live tenants, as `seed --reset` does; the lifecycle preconditions
# have their own tests below.
FORCE_DATA = "SELECT purge_tenant(%s, 'data', force => true)"
FORCE_FINAL = "SELECT purge_tenant(%s, 'final', force => true)"

Conn = psycopg.Connection[tuple[Any, ...]]

KEPT_BY_DATA = {"tenant_memberships", "audit_log"}


def _tenant_tables(db: Conn) -> list[str]:
    rows = db.execute(
        "SELECT c.table_name FROM information_schema.columns c "
        "JOIN information_schema.tables t USING (table_schema, table_name) "
        "WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id' "
        "AND t.table_type = 'BASE TABLE' ORDER BY 1"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _counts(db: Conn, tenant: UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _tenant_tables(db):
        query = sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(
            sql.Identifier(table)
        )
        row = db.execute(query, (tenant,)).fetchone()
        assert row is not None
        counts[table] = int(row[0])
    row = db.execute("SELECT count(*) FROM tenants WHERE id = %s", (tenant,)).fetchone()
    assert row is not None
    counts["tenants"] = int(row[0])
    return counts


def _users(db: Conn, tenant: UUID) -> list[UUID]:
    rows = db.execute(
        "SELECT user_id FROM tenant_memberships WHERE tenant_id = %s", (tenant,)
    ).fetchall()
    return [r[0] for r in rows]


def _published(db: Conn, tenant_id: UUID | None = None) -> Any:
    """A listing driven created → … → published through the guards (S8 order: job, then status)."""
    fl = flow(db)
    set_gucs(db, user_id=fl.operator)
    approved_flow(db, fl)
    force_state(db, fl.listing_id, "approved")
    job = queued_job(db, fl, "listing_publish")
    transition(db, fl.listing_id, "publishing", "publish")
    sub = submission(db, fl, job)
    db.execute(
        "UPDATE listing_submissions SET outcome = 'accepted', responded_at = now() WHERE id = %s",
        (sub,),
    )
    set_gucs(db, job_id=job)
    transition(
        db,
        fl.listing_id,
        "published",
        "submission_accepted",
        published_version_id=fl.v1,
        first_published_at="2026-10-06T00:00:00Z",
        marketplace_state="processing",
    )
    set_gucs(db, user_id=None, job_id=None)
    return fl


def _world(db: Conn) -> tuple[UUID, UUID]:
    """Tenant A: the full 0002 + 0003 graph (built with the submission and item guards off for
    that set-up only), a published listing, a brand profile, a cell, an invitation and an audit row
    with contact data; tenant B: the same graph."""
    for table, trigger in (("listing_submissions", "ls_guard"), ("listing_items", "li_guard")):
        db.execute(
            sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(
                sql.Identifier(table), sql.Identifier(trigger)
            )
        )
    a, b = _full_tenant(db), _full_tenant(db)
    for table, trigger in (("listing_submissions", "ls_guard"), ("listing_items", "li_guard")):
        db.execute(
            sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(
                sql.Identifier(table), sql.Identifier(trigger)
            )
        )
    for side in (a, b):
        style = f.product(db, side.tenant_id, "style", size_run=["s"])
        colourway = f.colourway(db, side.tenant_id, style)
        f.cell(db, side.tenant_id, style, colourway_id=colourway, size_key="s")
        party = f.party(db, side.tenant_id)
        f.brand_profile(db, side.tenant_id, default_manufacturer_party_id=party)
        f.insert(
            db,
            "invitations",
            tenant_id=side.tenant_id,
            email=f"new-{side.tenant_id}@x.test",
            role="reviewer",
            token_hash=bytes(side.tenant_id.bytes * 2),
            expires_at="2099-01-01T00:00:00Z",
        )
        f.audit(
            db,
            side.tenant_id,
            actor_type="user",
            actor_user_id=side.user_id,
            actor_email="owner@x.test",
            ip="10.0.0.1",
        )
    return a.tenant_id, b.tenant_id


def test_purge_tenant(db: Conn) -> None:
    a, b = _world(db)
    published = _published(db)
    before_b = _counts(db, b)
    users_a = _users(db, a)
    empty = sorted(t for t, n in _counts(db, a).items() if n == 0)
    assert empty == [], empty
    with as_migrator(db):
        db.execute(FORCE_DATA, (a,))
        db.execute(FORCE_DATA, (published.tenant_id,))
    after = _counts(db, a)
    assert {t: n for t, n in after.items() if n} == {
        "tenants": 1,
        "tenant_memberships": after["tenant_memberships"],
        "audit_log": after["audit_log"],
    }
    assert _counts(db, published.tenant_id)["listings"] == 0
    with as_migrator(db):
        db.execute(FORCE_FINAL, (a,))
    assert set(_counts(db, a).values()) == {0}
    left = db.execute("SELECT count(*) FROM users WHERE id = ANY(%s)", (users_a,)).fetchone()
    assert left == (0,)
    assert _counts(db, b) == before_b


def test_purge_final_needs_data_phase(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", FORCE_FINAL, (tenant,))  # force never skips the order
        expect_sqlstate(db, "GRD01", "SELECT purge_tenant(%s, 'everything')", (tenant,))


def test_purge_keeps_users_of_other_tenants(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    shared = f.member(db, a)
    f.insert(db, "tenant_memberships", tenant_id=b, user_id=shared, role="owner")
    only_a = f.member(db, a, role="operator")
    with as_migrator(db):
        db.execute(FORCE_DATA, (a,))
    rows: dict[Any, Any] = dict(
        db.execute(
            "SELECT id, erased_at IS NOT NULL FROM users WHERE id IN (%s, %s)", (shared, only_a)
        ).fetchall()
    )
    assert rows == {shared: False, only_a: True}
    with as_migrator(db):
        db.execute(FORCE_FINAL, (a,))
    remaining = {
        r[0]
        for r in db.execute(
            "SELECT id FROM users WHERE id IN (%s, %s)", (shared, only_a)
        ).fetchall()
    }
    assert remaining == {shared}


def test_purge_writes_tombstones(db: Conn) -> None:
    tenant = f.tenant(db)
    f.listing(db, tenant)
    with as_migrator(db):
        db.execute(FORCE_DATA, (tenant,))
        db.execute(FORCE_FINAL, (tenant,))
    rows = db.execute(
        "SELECT tenant_id, actor_type, after->>'mode', after->'row_counts', "
        "after ? 'backups_expire_by' FROM audit_log "
        "WHERE action = 'tenant.purged' AND entity_id = %s ORDER BY after->>'mode'",
        (tenant,),
    ).fetchall()
    assert [r[:3] for r in rows] == [(None, "system", "data"), (None, "system", "final")]
    assert rows[0][3]["listings"] == 1
    assert all(r[4] for r in rows)


def test_purge_nulls_audit_email_ip(db: Conn) -> None:
    tenant = f.tenant(db)
    user = f.member(db, tenant)
    row = f.audit(
        db, tenant, actor_type="user", actor_user_id=user, actor_email="a@x.test", ip="10.0.0.2"
    )
    with as_migrator(db):
        db.execute(FORCE_DATA, (tenant,))
    assert db.execute(
        "SELECT actor_email, ip, actor_user_id FROM audit_log WHERE id = %s", (row,)
    ).fetchone() == (None, None, user)


def test_purge_refused_for_app_rw(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        expect_sqlstate(db, "42501", "SELECT purge_tenant(%s, 'data')", (tenant,))
        expect_sqlstate(db, "42501", "SELECT purge_tenant_order()")


def test_purge_map_covers_tenant_tables(db: Conn) -> None:
    order = db.execute("SELECT purge_tenant_order()").fetchone()
    assert order is not None
    assert len(order[0]) == len(set(order[0]))
    assert set(order[0]) == set(_tenant_tables(db)) - KEPT_BY_DATA


# ---- lifecycle preconditions (DATABASE.md :1863-1873) ----


def _offboard(db: Conn, tenant: UUID, purge_after: str) -> None:
    """Offboard through the lifecycle function, then move purge_after (as the owner: no role can
    UPDATE it directly)."""
    with as_migrator(db):
        db.execute("SELECT set_tenant_lifecycle(%s, 'offboarded', 'test')", (tenant,))
    db.execute(
        f"UPDATE tenants SET purge_after = now() + interval '{purge_after}' WHERE id = %s",  # noqa: S608
        (tenant,),
    )


@pytest.mark.parametrize("status", ["trial", "active", "suspended"])
def test_purge_refused_for_active_tenant(db: Conn, status: str) -> None:
    trial = {"trial_ends_at": "2030-01-01"} if status == "trial" else {}
    tenant = f.tenant(db, status=status, **trial)
    f.listing(db, tenant)
    with as_migrator(db):
        err = expect_sqlstate(db, "GRD01", "SELECT purge_tenant(%s, 'data')", (tenant,))
    assert "offboarded" in str(err)
    assert _counts(db, tenant)["listings"] == 1


def test_purge_refused_before_purge_after(db: Conn) -> None:
    tenant = f.tenant(db)
    f.listing(db, tenant)
    _offboard(db, tenant, "1 day")
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", "SELECT purge_tenant(%s, 'data')", (tenant,))
    db.execute("UPDATE tenants SET purge_after = now() WHERE id = %s", (tenant,))
    with as_migrator(db):
        db.execute("SELECT purge_tenant(%s, 'data')", (tenant,))
    assert _counts(db, tenant)["listings"] == 0


def test_purge_final_refused_before_retention(db: Conn) -> None:
    tenant = f.tenant(db)
    _offboard(db, tenant, "-1 day")
    with as_migrator(db):
        db.execute("SELECT purge_tenant(%s, 'data')", (tenant,))
        err = expect_sqlstate(db, "GRD01", "SELECT purge_tenant(%s, 'final')", (tenant,))
    assert "retention" in str(err)
    db.execute(
        "UPDATE tenants SET data_purged_at = now() - interval '24 months' WHERE id = %s",
        (tenant,),
    )
    with as_migrator(db):
        db.execute("SELECT purge_tenant(%s, 'final')", (tenant,))
    assert db.execute("SELECT count(*) FROM tenants WHERE id = %s", (tenant,)).fetchone() == (0,)
