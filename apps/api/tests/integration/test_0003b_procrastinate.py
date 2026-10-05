"""Revision 0003b_procrastinate (DATABASE.md §5.4 "procrastinate tables", §8.2 ledger row 0003b;
ADR-0004 §1): procrastinate 3.10.0's own schema SQL, vendored and frozen, applied as `migrator`,
then EXECUTE for `app_rw` on every non-trigger procrastinate function. The vendored file must stay
byte-equal to the pinned library's SQL."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

VENDORED = (
    Path(__file__).resolve().parents[2] / "alembic" / "sql" / "procrastinate-3.10.0-schema.sql"
)
VENDORED_SHA256 = "c70ec4b400a60ad9592787653aae5ae77ae41bf801d56b5bd2712751a07f2009"
TABLES = (
    "procrastinate_events",
    "procrastinate_jobs",
    "procrastinate_periodic_defers",
    "procrastinate_workers",
)
FUNCTIONS = "SELECT p.oid, p.oid::regprocedure::text, p.prorettype = 'trigger'::regtype "
FUNCTIONS += "FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
FUNCTIONS += "AND p.proname LIKE 'procrastinate\\_%' ORDER BY 2"


def test_procrastinate_schema_matches(db: Conn) -> None:
    import procrastinate
    from procrastinate.schema import SchemaManager
    from procrastinate.sql import queries

    assert procrastinate.__version__ == "3.10.0"
    vendored = VENDORED.read_bytes()
    assert vendored == SchemaManager.get_schema().encode("utf-8")
    assert hashlib.sha256(vendored).hexdigest() == VENDORED_SHA256
    # the library's own check, run as the runtime role
    db.execute("SET LOCAL ROLE app_rw")
    row = db.execute(queries["check_connection"]).fetchone()
    assert row is not None and row[0] is not None


def test_procrastinate_objects_present(db: Conn) -> None:
    tables = db.execute(
        "SELECT relname, pg_get_userbyid(relowner), relrowsecurity FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
        "AND relname LIKE 'procrastinate\\_%' ORDER BY 1"
    ).fetchall()
    assert tables == [(name, "migrator", False) for name in TABLES]
    functions = db.execute(FUNCTIONS).fetchall()
    assert len(functions) == 18
    assert sum(1 for f in functions if not f[2]) == 11
    types = db.execute(
        "SELECT typname, typtype FROM pg_type WHERE typnamespace = 'public'::regnamespace "
        "AND typname LIKE 'procrastinate\\_%' AND (typtype = 'e' OR (typtype = 'c' "
        "AND typrelid IN (SELECT oid FROM pg_class WHERE relkind = 'c'))) ORDER BY 1"
    ).fetchall()
    assert types == [
        ("procrastinate_job_event_type", "e"),
        ("procrastinate_job_status", "e"),
        ("procrastinate_job_to_defer_v1", "c"),
    ]
    triggers = db.execute(
        "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE c.relname LIKE 'procrastinate\\_%' AND NOT t.tgisinternal"
    ).fetchone()
    assert triggers == (7,)


def test_procrastinate_grants(db: Conn) -> None:
    for oid, name, is_trigger in db.execute(FUNCTIONS).fetchall():
        execute = db.execute(
            "SELECT has_function_privilege('app_rw', %s::oid, 'EXECUTE')", (oid,)
        ).fetchone()
        assert execute == ((not is_trigger),), name
        public = db.execute(
            "SELECT count(*) FROM pg_proc p, "
            "aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
            "WHERE p.oid = %s AND a.grantee = 0",
            (oid,),
        ).fetchone()
        assert public == (0,), name
        for role in ("app_ro", "app_platform_ro"):
            granted = db.execute(
                "SELECT has_function_privilege(%s, %s::oid, 'EXECUTE')", (role, oid)
            ).fetchone()
            assert granted == (False,), (role, name)
    for table in TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            row = db.execute(
                "SELECT has_table_privilege('app_rw', %s, %s)", (f"public.{table}", privilege)
            ).fetchone()
            assert row == (True,), (table, privilege)


def test_readonly_roles_no_procrastinate(db: Conn) -> None:
    for table in TABLES:
        for role in ("app_ro", "app_platform_ro"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                row = db.execute(
                    "SELECT has_table_privilege(%s, %s, %s)", (role, f"public.{table}", privilege)
                ).fetchone()
                assert row == (False,), (role, table, privilege)


def test_procrastinate_defer_as_app_rw(db: Conn) -> None:
    db.execute("SET LOCAL ROLE app_rw")
    args = json.dumps({"job_run_id": str(uuid4())})
    deferred = db.execute(
        "SELECT procrastinate_defer_jobs_v1(ARRAY[ROW('default', 'probe', 0, NULL, NULL, "
        "%s::jsonb, NULL)::procrastinate_job_to_defer_v1])",
        (args,),
    ).fetchone()
    assert deferred is not None
    (ids,) = deferred
    assert len(ids) == 1
    fetched = db.execute(
        "SELECT id, status, args FROM procrastinate_fetch_job_v2(ARRAY['default'], NULL)"
    ).fetchone()
    assert fetched is not None and fetched[0] == ids[0] and fetched[1] == "doing"
    assert fetched[2] == json.loads(args)
    db.execute("SELECT procrastinate_finish_job_v1(%s, 'succeeded', false)", (ids[0],))
    status = db.execute("SELECT status FROM procrastinate_jobs WHERE id = %s", (ids[0],))
    assert status.fetchone() == ("succeeded",)


def test_procrastinate_percent_survives(db: Conn) -> None:
    row = db.execute("SELECT pg_get_functiondef('procrastinate_finish_job_v1'::regproc)").fetchone()
    assert row is not None
    assert "(job id: %)" in row[0]
    assert "%%" not in row[0]
