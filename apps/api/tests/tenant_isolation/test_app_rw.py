"""The isolation suite logs in as `app_rw`, never a superuser (KNOWN-ISSUES.md:30; DATABASE.md:129)."""

from __future__ import annotations

import psycopg
import pytest


def test_isolation_runs_as_app_rw(app_conn: psycopg.Connection[tuple[object, ...]]) -> None:
    row = app_conn.execute(
        "SELECT session_user, current_user, current_database(), r.rolsuper, r.rolbypassrls "
        "FROM pg_roles r WHERE r.rolname = current_user"
    ).fetchone()
    assert row == ("app_rw", "app_rw", "mos_iso", False, False)


def test_app_rw_cannot_set_role_postgres(app_conn: psycopg.Connection[tuple[object, ...]]) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("SET ROLE postgres")


def test_app_rw_inherits_no_superuser_or_bypassrls_role(
    app_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """No role app_rw is a member of (transitively) may carry rolsuper or rolbypassrls."""
    rows = app_conn.execute(
        "SELECT r.rolname FROM pg_roles r "
        "WHERE pg_has_role(current_user, r.oid, 'MEMBER') AND (r.rolsuper OR r.rolbypassrls)"
    ).fetchall()
    assert rows == []


def test_app_rw_owns_no_tenant_table(app_conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """Table owners bypass RLS without FORCE; app_rw must own nothing in public."""
    rows = app_conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
        "AND pg_has_role(current_user, c.relowner, 'USAGE')"
    ).fetchall()
    assert rows == []


def test_suite_database_is_migrated(app_conn: psycopg.Connection[tuple[object, ...]]) -> None:
    row = app_conn.execute("SELECT to_regclass('public.listings') IS NOT NULL").fetchone()
    assert row == (True,)
