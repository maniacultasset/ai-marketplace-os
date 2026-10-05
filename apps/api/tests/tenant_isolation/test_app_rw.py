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
