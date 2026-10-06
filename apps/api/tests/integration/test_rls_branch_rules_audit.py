"""0005's `rules` and `audit_log` policies as app_rw (DATABASE.md:508, :304). `rules` reads platform
rows (NULL tenant) and its own, writes and toggles only its own; `audit_log` reads its own tenant
only and inserts its own or NULL-tenant rows (failed logins), so `audit()` works without the row
being readable back: it inserts without RETURNING."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, set_gucs
from .test_0004_audit import call_audit

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

BRAND_RULE_SQL = (
    "INSERT INTO rules (tenant_id, marketplace, field, rule_type, subject, pack, rule_def) "
    "VALUES (%s, 'amazon', 'content.title', 'banned_terms', '', 'brand', %s)"
)
RAW_AUDIT_SQL = (
    "INSERT INTO audit_log (tenant_id, actor_type, action, entity_type) "
    "VALUES (%s, 'system', 'listing.approved', 'listing')"
)


def _brand_def() -> Jsonb:
    return Jsonb(f.rule_def("banned_terms", terms=["best"], pack="brand"))


def _ids(conn: Conn, query: str, params: Any = None) -> set[UUID]:
    return {row[0] for row in conn.execute(query, params).fetchall()}


def test_rls_branch_rules_read_platform_and_own(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    platform, own, other = f.rule(db, None), f.rule(db, a), f.rule(db, b)
    set_gucs(db, tenant_id=a)
    with as_app_rw(db):
        seen = _ids(db, "SELECT id FROM rules WHERE id IN (%s, %s, %s)", [platform, own, other])
        assert seen == {platform, own}
    set_gucs(db, tenant_id=None)
    with as_app_rw(db):
        seen = _ids(db, "SELECT id FROM rules WHERE id IN (%s, %s, %s)", [platform, own, other])
        assert seen == {platform}


def test_rls_branch_rules_write_own_only(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    set_gucs(db, tenant_id=a)
    with as_app_rw(db):
        db.execute(BRAND_RULE_SQL, [a, _brand_def()])
        expect_sqlstate(db, "42501", BRAND_RULE_SQL, [b, _brand_def()])
        expect_sqlstate(db, "42501", BRAND_RULE_SQL, [None, _brand_def()])


def test_rls_branch_rules_toggle_own_only(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    own, other = f.rule(db, a), f.rule(db, b)
    set_gucs(db, tenant_id=a)
    with as_app_rw(db):
        toggle = "UPDATE rules SET enabled = false WHERE id = %s"
        assert db.execute(toggle, [other]).rowcount == 0
        assert db.execute(toggle, [own]).rowcount == 1


def test_rls_branch_rules_platform_toggle_refused(db: Conn) -> None:
    a = f.tenant(db)
    platform = f.rule(db, None)
    set_gucs(db, tenant_id=a)
    with as_app_rw(db):
        assert (
            db.execute("UPDATE rules SET enabled = false WHERE id = %s", [platform]).rowcount == 0
        )
    enabled = db.execute("SELECT enabled FROM rules WHERE id = %s", [platform]).fetchone()
    assert enabled == (True,)


def test_rls_branch_audit_insert_null_tenant_ok(db: Conn) -> None:
    """A failed login for an unknown email has no tenant: audit() still writes its row."""
    set_gucs(db, tenant_id=None)
    with as_app_rw(db):
        request_id = call_audit(db, None, entity_type="user", action="auth.login_failed")
    count = db.execute("SELECT count(*) FROM audit_log WHERE request_id = %s", [request_id])
    assert count.fetchone() == (1,)


def test_rls_branch_audit_insert_other_tenant_refused(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    set_gucs(db, tenant_id=a)
    with as_app_rw(db):
        call_audit(db, a)
        try:
            with db.transaction():
                call_audit(db, b)
        except psycopg.errors.InsufficientPrivilege as error:
            assert "row-level security" in str(error)
        else:  # pragma: no cover - the failure
            raise AssertionError("audit() wrote a row for another tenant")
        expect_sqlstate(db, "42501", RAW_AUDIT_SQL, [b])


def test_rls_branch_audit_select_own_only(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    own, other, platform = f.audit(db, a), f.audit(db, b), f.audit(db, None)
    set_gucs(db, tenant_id=a)
    with as_app_rw(db):
        seen = _ids(db, "SELECT id FROM audit_log WHERE id IN (%s, %s, %s)", [own, other, platform])
        assert seen == {own}
        # no UPDATE policy (and no UPDATE grant): nothing is found to change
        expect_sqlstate(db, "42501", "UPDATE audit_log SET reason = 'x' WHERE id = %s", [own])


def test_rls_branch_audit_fn_works_without_select(db: Conn) -> None:
    """The NULL-tenant row is not readable back by app_rw, so a RETURNING insert must fail the
    SELECT policy (42501) while audit(), which inserts without RETURNING, succeeds."""
    a = f.tenant(db)
    set_gucs(db, tenant_id=a)
    with as_app_rw(db):
        call_audit(db, None, entity_type="user", action="auth.login_failed")
        db.execute(RAW_AUDIT_SQL, [None])  # the plain insert passes the INSERT policy
        expect_sqlstate(db, "42501", RAW_AUDIT_SQL + " RETURNING id", [None])
