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
