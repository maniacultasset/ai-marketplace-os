"""AC8 against a real Postgres: scripts/db/0000_bootstrap.sql is idempotent and complete."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

Conn = psycopg.Connection[tuple[Any, ...]]

# pytest-socket blocks socket.getaddrinfo, which psycopg calls to resolve the host before libpq
# connects. Database tests may resolve and reach the loopback test database only.
pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])


@pytest.fixture
def bootstrap_sql(repo_root: Path) -> str:
    # A committed file, not user input: safe to run as a script.
    return (repo_root / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")


def _scalar(conn: Conn, query: str) -> Any:
    row = conn.execute(query).fetchone()
    assert row is not None
    return row[0]


def test_runs_twice_without_error(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    superuser_conn.execute(bootstrap_sql)


def test_extensions_present(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    rows = superuser_conn.execute("SELECT extname FROM pg_extension").fetchall()
    assert {"vector", "pgcrypto", "pg_trgm"} <= {row[0] for row in rows}


def test_vector_version_at_least_0_8_2(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    version = _scalar(superuser_conn, "SELECT extversion FROM pg_extension WHERE extname='vector'")
    assert tuple(int(part) for part in version.split(".")) >= (0, 8, 2)


def test_read_only_roles_exist(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    rows = superuser_conn.execute(
        "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls "
        "FROM pg_roles WHERE rolname IN "
        "('migrator', 'app_rw', 'app_ro', 'app_platform_ro')"
    ).fetchall()
    found = {row[0]: row[1:] for row in rows}
    assert set(found) == {"migrator", "app_rw", "app_ro", "app_platform_ro"}
    for flags in found.values():
        assert flags[1] is False  # rolsuper
        assert flags[2] is False  # rolbypassrls
    assert found["app_platform_ro"][0] is False
    assert found["app_ro"][0] is True
    assert found["migrator"][0] is True
    assert found["app_rw"][0] is True


def test_app_ro_is_read_only_by_default(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    readonly = _scalar(
        superuser_conn,
        "SELECT rolconfig FROM pg_roles WHERE rolname = 'app_ro'",
    )
    assert readonly is not None
    assert "default_transaction_read_only=on" in readonly


def test_timeouts_and_utc(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    migrator = _scalar(superuser_conn, "SELECT rolconfig FROM pg_roles WHERE rolname = 'migrator'")
    app_rw = _scalar(superuser_conn, "SELECT rolconfig FROM pg_roles WHERE rolname = 'app_rw'")
    assert "lock_timeout=5s" in migrator
    assert "idle_in_transaction_session_timeout=60s" in app_rw
    tz = _scalar(
        superuser_conn,
        "SELECT setconfig FROM pg_db_role_setting s "
        "JOIN pg_database d ON d.oid = s.setdatabase "
        "WHERE d.datname = current_database() AND s.setrole = 0",
    )
    assert tz is not None
    assert any(item.lower() == "timezone=utc" for item in tz)


def test_roles_exist_with_login(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    rows = superuser_conn.execute(
        "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname IN ('migrator', 'app_rw')"
    ).fetchall()
    assert {row[0]: row[1] for row in rows} == {"migrator": True, "app_rw": True}


def test_public_schema_owned_by_migrator(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    owner = _scalar(
        superuser_conn, "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname='public'"
    )
    assert owner == "migrator"


def test_default_privileges_grant_app_rw(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    rows = superuser_conn.execute(
        "SELECT d.defaclobjtype, d.defaclacl::text FROM pg_default_acl d "
        "JOIN pg_namespace n ON n.oid = d.defaclnamespace "
        "WHERE d.defaclrole = 'migrator'::regrole AND n.nspname = 'public'"
    ).fetchall()
    acl = {row[0]: row[1] for row in rows}
    assert "app_rw=arwd/migrator" in acl["r"]  # tables: SELECT, INSERT, UPDATE, DELETE
    assert "app_rw=rU/migrator" in acl["S"]  # sequences: SELECT, USAGE


def test_rerun_repairs_partial_state(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    superuser_conn.execute("DROP EXTENSION pg_trgm")
    superuser_conn.execute(bootstrap_sql)
    assert _scalar(superuser_conn, "SELECT count(*) FROM pg_extension WHERE extname='pg_trgm'") == 1


def test_bootstrap_sql_memberships(superuser_conn: Conn, bootstrap_sql: str) -> None:
    """DATABASE.md:1739, :1752: app_rw may SET app_platform_ro but never inherits it."""
    superuser_conn.execute(bootstrap_sql)
    rows = superuser_conn.execute(
        "SELECT m.member::regrole::text, m.inherit_option, m.set_option "
        "FROM pg_auth_members m WHERE m.roleid = 'app_platform_ro'::regrole"
    ).fetchall()
    rw = [row[1:] for row in rows if row[0] == "app_rw"]
    ro = [row[1:] for row in rows if row[0] == "app_ro"]
    assert rw
    assert ro
    assert all(inherit is False and set_ is True for inherit, set_ in rw)
    assert all(inherit is True for inherit, _ in ro)
    into_migrator = superuser_conn.execute(
        "SELECT m.member::regrole::text FROM pg_auth_members m "
        "WHERE m.roleid = 'migrator'::regrole AND m.member::regrole::text IN ('app_rw', 'app_ro')"
    ).fetchall()
    assert into_migrator == []


def test_read_only_roles_get_no_default_select(superuser_conn: Conn, bootstrap_sql: str) -> None:
    """DATABASE.md:1750: only app_rw gets default table and sequence privileges."""
    superuser_conn.execute(bootstrap_sql)
    with superuser_conn.transaction(force_rollback=True):
        superuser_conn.execute("SET LOCAL ROLE migrator")
        superuser_conn.execute("CREATE TABLE public.s5_probe_table (id int)")
        superuser_conn.execute("CREATE SEQUENCE public.s5_probe_seq")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert _scalar(
                superuser_conn,
                f"SELECT has_table_privilege('app_rw', 'public.s5_probe_table', '{privilege}')",
            ), privilege
        for role in ("app_ro", "app_platform_ro"):
            assert not _scalar(
                superuser_conn,
                f"SELECT has_table_privilege('{role}', 'public.s5_probe_table', 'SELECT')",
            ), role
        assert _scalar(
            superuser_conn,
            "SELECT has_sequence_privilege('app_rw', 'public.s5_probe_seq', 'USAGE')",
        )


def test_functions_not_executable_by_public(superuser_conn: Conn, bootstrap_sql: str) -> None:
    """DATABASE.md:1752: a function migrator creates is not executable by PUBLIC or the app."""
    superuser_conn.execute(bootstrap_sql)
    with superuser_conn.transaction(force_rollback=True):
        superuser_conn.execute("CREATE ROLE s5_public_probe NOLOGIN")
        superuser_conn.execute("SET LOCAL ROLE migrator")
        superuser_conn.execute(
            "CREATE FUNCTION public.s5_probe_fn() RETURNS int LANGUAGE sql AS 'SELECT 1'"
        )
        acl = _scalar(
            superuser_conn,
            "SELECT proacl::text FROM pg_proc WHERE oid = 'public.s5_probe_fn()'::regprocedure",
        )
        assert acl == "{migrator=X/migrator}"
        for role in ("app_rw", "app_ro", "s5_public_probe"):
            assert not _scalar(
                superuser_conn,
                f"SELECT has_function_privilege('{role}', 'public.s5_probe_fn()', 'EXECUTE')",
            ), role


def test_schema_usage_for_app_roles(superuser_conn: Conn, bootstrap_sql: str) -> None:
    superuser_conn.execute(bootstrap_sql)
    for role in ("app_rw", "app_ro", "app_platform_ro"):
        assert _scalar(superuser_conn, f"SELECT has_schema_privilege('{role}', 'public', 'USAGE')")
        assert not _scalar(
            superuser_conn, f"SELECT has_schema_privilege('{role}', 'public', 'CREATE')"
        ), role


def _public_objects(conn: Conn) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Relations and functions in `public` that are not extension members."""
    relations = conn.execute(
        "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT EXISTS (SELECT 1 FROM pg_depend d "
        "WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e') "
        "ORDER BY 1, 2"
    ).fetchall()
    functions = conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass "
        "AND d.objid = p.oid AND d.deptype = 'e') ORDER BY 1"
    ).fetchall()
    return relations, functions


def test_bootstrap_adds_no_relation_or_function(superuser_conn: Conn, bootstrap_sql: str) -> None:
    """0000 creates roles, extensions and privileges only. `make api-tests` leaves mos_test at
    the Alembic head before pytest, so the check compares before and after, never "empty"."""
    before = _public_objects(superuser_conn)
    superuser_conn.execute(bootstrap_sql)
    superuser_conn.execute(bootstrap_sql)
    assert _public_objects(superuser_conn) == before
