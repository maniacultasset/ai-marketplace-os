"""0004/03_append_only (DATABASE.md §5.2 audit_log "Append-only", §9 :1847-1849, :1871-1874): the
four append-only tables refuse UPDATE, DELETE and TRUNCATE for `app_rw` by privilege, and for
everyone else by trigger, unless the session user is `migrator` and a matching GUC is set."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, as_migrator, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

TABLES = ("audit_log", "agent_runs", "validation_runs", "review_decisions")
# a column each table has, for a harmless UPDATE
TOUCH = {
    "audit_log": "reason",
    "agent_runs": "prompt_version",
    "validation_runs": "request_id",
    "review_decisions": "request_id",
}


def _row(conn: Conn, table: str) -> tuple[UUID, UUID]:
    """One row of `table` in a fresh tenant: (tenant_id, row id)."""
    tenant = f.tenant(conn)
    makers: dict[str, Callable[[], UUID]] = {
        "audit_log": lambda: f.audit(conn, tenant),
        "agent_runs": lambda: f.agent_run(conn, tenant),
        "validation_runs": lambda: f.validation_run(conn, tenant, f.listing(conn, tenant)),
        "review_decisions": lambda: _decision(conn, tenant),
    }
    return tenant, makers[table]()


def _decision(conn: Conn, tenant: UUID) -> UUID:
    listing = f.listing(conn, tenant)
    return f.review_decision(conn, tenant, listing, f.version(conn, tenant, listing))


def _update(table: str) -> sql.Composed:
    return sql.SQL("UPDATE {} SET {} = 'x' WHERE id = %s").format(
        sql.Identifier(table), sql.Identifier(TOUCH[table])
    )


def _delete(table: str) -> sql.Composed:
    return sql.SQL("DELETE FROM {} WHERE id = %s").format(sql.Identifier(table))


def _truncate(table: str) -> sql.Composed:
    return sql.SQL("TRUNCATE {}").format(sql.Identifier(table))


def test_audit_append_only(db: Conn) -> None:
    _, row = _row(db, "audit_log")
    with as_app_rw(db):
        expect_sqlstate(db, "42501", _update("audit_log"), (row,))
        expect_sqlstate(db, "42501", _delete("audit_log"), (row,))
        expect_sqlstate(db, "42501", _truncate("audit_log"))


@pytest.mark.parametrize("table", TABLES)
def test_append_only_tables(db: Conn, table: str) -> None:
    _, row = _row(db, table)
    with as_app_rw(db):
        expect_sqlstate(db, "42501", _update(table), (row,))
        expect_sqlstate(db, "42501", _delete(table), (row,))
        expect_sqlstate(db, "42501", _truncate(table))
        privileges = db.execute(
            "SELECT has_table_privilege('app_rw', %s, 'UPDATE'), "
            "has_table_privilege('app_rw', %s, 'DELETE'), "
            "has_table_privilege('app_rw', %s, 'TRUNCATE'), "
            "has_table_privilege('app_rw', %s, 'INSERT'), "
            "has_table_privilege('app_rw', %s, 'SELECT')",
            (table,) * 5,
        ).fetchone()
    assert privileges == (False, False, False, True, True)


@pytest.mark.parametrize("table", TABLES)
def test_append_only_trigger_refuses_migrator_without_guc(db: Conn, table: str) -> None:
    """The owner holds every privilege, so only the trigger stands in its way."""
    tenant, row = _row(db, table)
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", _update(table), (row,))
        expect_sqlstate(db, "GRD01", _delete(table), (row,))
        expect_sqlstate(db, "GRD01", _truncate(table))
        set_gucs(db, prune_table="audit_log" if table != "audit_log" else "agent_runs")
        expect_sqlstate(db, "GRD01", _delete(table), (row,))
        set_gucs(db, prune_table=None, purge_tenant=UUID(int=1))
        expect_sqlstate(db, "GRD01", _delete(table), (row,))
        set_gucs(db, purge_tenant=tenant)
        expect_sqlstate(db, "GRD01", _truncate(table))


@pytest.mark.parametrize("table", TABLES)
def test_append_only_allows_migrator_with_purge_guc(db: Conn, table: str) -> None:
    tenant, row = _row(db, table)
    with as_migrator(db):
        set_gucs(db, purge_tenant=tenant)
        # purge only deletes, and nulls audit email and ip (below): any other UPDATE is refused
        expect_sqlstate(db, "GRD01", _update(table), (row,))
        db.execute(_delete(table), (row,))
    assert db.execute(
        sql.SQL("SELECT count(*) FROM {} WHERE id = %s").format(sql.Identifier(table)), (row,)
    ).fetchone() == (0,)


@pytest.mark.parametrize("table", TABLES)
def test_append_only_allows_migrator_prune_delete_only(db: Conn, table: str) -> None:
    _, row = _row(db, table)
    with as_migrator(db):
        set_gucs(db, prune_table=table)
        expect_sqlstate(db, "GRD01", _update(table), (row,))
        db.execute(_delete(table), (row,))


def test_append_only_redact_updates_own_audit_rows(db: Conn) -> None:
    tenant = f.tenant(db)
    user = f.member(db, tenant)
    other = f.member(db, tenant)
    mine = f.audit(db, tenant, actor_type="user", actor_user_id=user, actor_email="a@x.test")
    theirs = f.audit(db, tenant, actor_type="user", actor_user_id=other, actor_email="b@x.test")
    with as_migrator(db):
        set_gucs(db, redact_user=user)
        db.execute("UPDATE audit_log SET actor_email = NULL WHERE id = %s", (mine,))
        expect_sqlstate(
            db, "GRD01", "UPDATE audit_log SET actor_email = NULL WHERE id = %s", (theirs,)
        )
        expect_sqlstate(db, "GRD01", "DELETE FROM audit_log WHERE id = %s", (mine,))


@pytest.mark.parametrize("table", TABLES)
def test_append_only_refuses_superuser_with_guc(db: Conn, table: str) -> None:
    tenant, row = _row(db, table)
    set_gucs(db, purge_tenant=tenant, prune_table=table)
    expect_sqlstate(db, "GRD01", _update(table), (row,))
    expect_sqlstate(db, "GRD01", _delete(table), (row,))
    expect_sqlstate(db, "GRD01", _truncate(table))


def test_append_only_insert_allowed(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        f.audit(db, tenant)
        f.agent_run(db, tenant)
    listing = f.listing(db, tenant)
    version = f.version(db, tenant, listing)
    with as_app_rw(db):
        f.validation_run(db, tenant, listing)
        f.review_decision(db, tenant, listing, version, decided_by_user_id=f.member(db, tenant))


NULL_OUT = "UPDATE audit_log SET actor_email = NULL, ip = NULL WHERE id = %s"
FORGERIES = (
    "UPDATE audit_log SET action = 'forged.action' WHERE id = %s",
    "UPDATE audit_log SET actor_email = NULL, reason = 'forged' WHERE id = %s",
    "UPDATE audit_log SET actor_email = 'someone@else.test' WHERE id = %s",
    "UPDATE audit_log SET actor_user_id = NULL WHERE id = %s",
)


@pytest.mark.parametrize("forgery", FORGERIES)
@pytest.mark.parametrize("guc", ["redact_user", "purge_tenant"])
def test_append_only_audit_updates_only_null_email_and_ip(db: Conn, guc: str, forgery: str) -> None:
    """redact_user and purge_tenant only null actor_email and ip (0004/18, 0004/19): under either
    GUC the migrator cannot rewrite any other audit column, or set an email."""
    tenant = f.tenant(db)
    user = f.member(db, tenant)
    row = f.audit(db, tenant, actor_type="user", actor_user_id=user, actor_email="a@x.test")
    with as_migrator(db):
        set_gucs(db, **{guc: user if guc == "redact_user" else tenant})
        expect_sqlstate(db, "GRD01", forgery, (row,))
        db.execute(NULL_OUT, (row,))
    assert db.execute(
        "SELECT actor_email, ip, action FROM audit_log WHERE id = %s", (row,)
    ).fetchone() == (None, None, "listing.approved")
