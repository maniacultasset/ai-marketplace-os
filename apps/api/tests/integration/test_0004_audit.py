"""0004/02_audit (DATABASE.md §5.2 audit_log, §4 "Helper grants"; CODE-PLAN Q1): `audit()` filters
`before`/`after` to the per-entity allow-list, inserts one row without RETURNING, and raises for an
entity type with no entry. The allow-list below is written from the plan's Q1 table."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from app.repositories.audit import AUDITED_ENTITIES
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

ALLOWLIST: dict[str, set[str]] = {
    "product": {"attributes", "provenance"},
    "brand_profile": {"provenance"},
    "listing": {"status", "status_reason", "paused_at", "archived_at"},
    "rules": {"enabled"},
    "membership": {"role", "is_active"},
    "tenant": {"name", "tone", "guidelines", "glossary"},
    "marketplace_account": {"status", "settings"},
    "business_party": {"roles", "source"},
    "category_template": {"marketplace", "category"},
    "job_run": {"kind", "status", "parent_job_id"},
    "image": {"slot", "status", "deleted_at"},
    "listing_issue": {"status", "resolution"},
    "marketplace_error_map": {"enabled"},
    "brand_document": set(),
    "user": set(),
}
AUDIT_SQL = "SELECT audit(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"


def call_audit(conn: Conn, tenant_id: UUID | None, **kw: Any) -> str:
    """Call `audit()`; returns the row's (unique) `request_id`."""
    values = {
        "actor_type": "system",
        "actor_user_id": None,
        "action": "membership.role_changed",
        "entity_type": "membership",
        "entity_id": None,
        "before": None,
        "after": None,
        "reason": None,
        "request_id": f"req-{uuid4()}",
        "job_id": None,
        "ip": None,
        "actor_email": None,
        **kw,
    }
    for key in ("before", "after"):
        if values[key] is not None:
            values[key] = Jsonb(values[key])
    conn.execute(
        AUDIT_SQL,
        (
            tenant_id,
            values["actor_type"],
            values["actor_user_id"],
            values["action"],
            values["entity_type"],
            values["entity_id"],
            values["before"],
            values["after"],
            values["reason"],
            values["request_id"],
            values["job_id"],
            values["ip"],
            values["actor_email"],
        ),
    )
    return str(values["request_id"])


def _rows(conn: Conn, request_id: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT tenant_id, actor_type, actor_user_id, action, entity_type, before, after, "
        "request_id FROM audit_log WHERE request_id = %s",
        (request_id,),
    ).fetchall()


def _row(conn: Conn, request_id: str) -> tuple[Any, ...]:
    rows = _rows(conn, request_id)
    assert len(rows) == 1, rows
    return rows[0]


def test_audit_inserts_one_row(db: Conn) -> None:
    tenant = f.tenant(db)
    owner = f.member(db, tenant)
    total = db.execute("SELECT count(*) FROM audit_log").fetchone()
    with as_app_rw(db):
        rid = call_audit(db, tenant, actor_type="user", actor_user_id=owner, entity_id=owner)
    assert db.execute("SELECT count(*) FROM audit_log").fetchone() == (total[0] + 1,)  # type: ignore[index]
    row = _row(db, rid)
    assert row[:5] == (tenant, "user", owner, "membership.role_changed", "membership")
    assert row[7] == rid


def test_audit_filters_to_allowlist(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        rid = call_audit(
            db,
            tenant,
            before={"role": "operator", "is_active": True, "email": "a@x.test"},
            after={"role": "owner", "is_active": True, "email": "a@x.test", "phone": "+91 99"},
        )
    row = _row(db, rid)
    assert row[5] == {"role": "operator", "is_active": True}
    assert row[6] == {"role": "owner", "is_active": True}


def test_audit_null_before_stays_null(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        rid = call_audit(db, tenant, after={"email": "a@x.test"})
    row = _row(db, rid)
    assert row[5] is None
    assert row[6] == {}


def test_audit_unknown_entity_raises(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "GRD01",
            AUDIT_SQL,
            (
                tenant,
                "system",
                None,
                "x.y",
                "invitation",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        )


def test_audit_null_tenant_system_ok(db: Conn) -> None:
    with as_app_rw(db):
        rid = call_audit(db, None, action="auth.login_failed", entity_type="user")
    assert _row(db, rid)[:2] == (None, "system")


def test_audit_null_tenant_user_actor_rejected(db: Conn) -> None:
    tenant = f.tenant(db)
    owner = f.member(db, tenant)
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "23514",
            AUDIT_SQL,
            (None, "user", owner, "x.y", "user", None, None, None, None, None, None, None, None),
            constraint="ck_audit_log_platform_actor",
        )


def test_audit_staff_needs_email(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "23514",
            AUDIT_SQL,
            (
                tenant,
                "staff",
                None,
                "tenant.created",
                "tenant",
                tenant,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
            constraint="ck_audit_log_staff_actor",
        )
        call_audit(db, tenant, actor_type="staff", entity_type="tenant", actor_email="ops@x.test")


def test_audit_execute_grants(db: Conn) -> None:
    for function in ("audit", "audit_allowlist", "audit_unknown_entity"):
        rows = db.execute(
            "SELECT r.rolname, has_function_privilege(r.rolname, p.oid, 'EXECUTE') "
            "FROM pg_proc p, pg_roles r WHERE p.proname = %s "
            "AND r.rolname IN ('app_rw', 'app_ro', 'app_platform_ro')",
            (function,),
        ).fetchall()
        assert dict(rows) == {"app_rw": True, "app_ro": False, "app_platform_ro": False}, function
        public = db.execute(
            "SELECT p.proacl IS NULL OR EXISTS (SELECT 1 FROM aclexplode(p.proacl) a "
            "WHERE a.grantee = 0) FROM pg_proc p WHERE p.proname = %s",
            (function,),
        ).fetchone()
        assert public == (False,), f"PUBLIC holds EXECUTE on {function}"


@pytest.mark.parametrize("entity", sorted(ALLOWLIST))
def test_audit_allowlist_exact(db: Conn, entity: str) -> None:
    row = db.execute("SELECT audit_allowlist(%s)", (entity,)).fetchone()
    assert row is not None
    assert set(row[0]) == ALLOWLIST[entity]
    assert len(row[0]) == len(ALLOWLIST[entity])


@pytest.mark.parametrize("entity", ["brand_document", "user"])
def test_audit_empty_allowlist_entity_ok(db: Conn, entity: str) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        rid = call_audit(db, tenant, entity_type=entity, before={}, after={"filename": "x.pdf"})
    row = _row(db, rid)
    assert row[4:7] == (entity, {}, {})


def test_audit_allowlist_covers_audited_entities(db: Conn) -> None:
    assert frozenset(ALLOWLIST) == AUDITED_ENTITIES
    for entity in sorted(AUDITED_ENTITIES):
        with db.transaction():
            db.execute("SELECT audit_allowlist(%s)", (entity,))
