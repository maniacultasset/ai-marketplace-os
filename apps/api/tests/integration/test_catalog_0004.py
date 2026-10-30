"""Catalog assertions for 0004 (DATABASE.md §8.2 "Proposed catalog assertions", §8.1): what
`alembic check` cannot see. Plus a guard refusal surfacing from S9's unit of work as its
DomainError."""

from __future__ import annotations

import re
from typing import Any

import psycopg
import pytest

from . import factories as f
from .integrity_support import next_version

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

SECRET = re.compile(r"(token|hash|password|secret|verifier|_enc)$")
# DATABASE.md §8.1: the contact and free-text columns the read-only roles never read
NAMED_PERSONAL = {
    ("business_parties", "email"),
    ("business_parties", "phone"),
    ("business_parties", "address_line1"),
    ("business_parties", "address_line2"),
    ("business_parties", "postal_code"),
    ("business_parties", "gstin"),
    ("business_parties", "dedupe_key"),
    ("import_rows", "raw_row"),
    ("human_tasks", "answer"),
    ("human_tasks", "suggestions"),
    ("human_tasks", "evidence"),
}
PLATFORM = (
    "category_templates",
    "marketplace_error_map",
    "attribute_definitions",
    "attribute_columns",
    "category_attributes",
    "listing_status_transitions",
)
APPEND_ONLY = ("audit_log", "agent_runs", "validation_runs", "review_decisions")
HELPERS = (
    "app_tenant_id()",
    "app_user_id()",
    "app_login_email()",
    "app_job_id()",
    "app_token_hash()",
    "fact_map_valid(jsonb,boolean)",
    "gtin_valid(text)",
    "audit(uuid,text,uuid,text,text,uuid,jsonb,jsonb,text,text,uuid,inet,text)",
    "set_tenant_lifecycle(uuid,text,text)",
    "expire_trials()",
)
MIGRATOR_ONLY = (
    "purge_tenant(uuid,text,boolean)",
    "purge_tenant_order()",
    "redact_user(uuid)",
    "prune_audit_log(timestamp with time zone)",
    "prune_append_only(regclass,timestamp with time zone)",
    "prune_closed(regclass,timestamp with time zone)",
)
OWN_FUNCTIONS = (
    "SELECT p.oid::regprocedure::text, p.oid FROM pg_proc p "
    "WHERE p.pronamespace = 'public'::regnamespace AND NOT EXISTS ("
    "SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid "
    "AND d.deptype = 'e')"
)


def _signature(name: str) -> str:
    return name.replace(" ", "").replace("timestampwithtimezone", "timestamp with time zone")


def test_catalog_no_public_execute(db: Conn) -> None:
    rows = db.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "WHERE p.pronamespace = 'public'::regnamespace AND NOT EXISTS ("
        "SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid "
        "AND d.deptype = 'e') AND EXISTS (SELECT 1 FROM "
        "aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a WHERE a.grantee = 0)"
    ).fetchall()
    assert rows == []


def test_catalog_definer_search_path(db: Conn) -> None:
    rows = db.execute(
        "SELECT p.oid::regprocedure::text, p.proconfig FROM pg_proc p "
        "WHERE p.pronamespace = 'public'::regnamespace AND p.prosecdef ORDER BY 1"
    ).fetchall()
    assert [r[0] for r in rows] == [
        "app_begin_auth_attempt(uuid)",  # 0011 (DPP-1)
        "app_clear_auth_failures(uuid)",  # 0011 (DPP-1)
        "app_create_google_applicant(text,text,text,text,text,text,text)",  # 0012 (ADR-0020)
        "app_google_user(text)",  # 0012
        "app_is_platform_admin()",  # 0010 (ADR-0019)
        "app_link_google_sub(uuid,text)",  # 0012
        "app_note_auth_failure(uuid)",  # 0011 (DPP-1)
        "app_prune_login_flows()",  # 0012
        "app_unlink_google_sub(uuid)",  # 0012
        "expire_trials()",
        "set_tenant_lifecycle(uuid,text,text)",
    ]
    for name, config in rows:
        expected = ["search_path=pg_catalog, public, pg_temp"]
        if name == "app_begin_auth_attempt(uuid)":  # 0011: the row-lock wait is bounded
            expected.append("lock_timeout=5s")
        assert config == expected, name


def test_catalog_readonly_roles_no_secret_columns(db: Conn) -> None:
    rows = db.execute(
        "SELECT c.relname, a.attname, r.rolname FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "CROSS JOIN (VALUES ('app_ro'), ('app_platform_ro')) AS r(rolname) "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'v') "
        "AND a.attnum > 0 AND NOT a.attisdropped "
        "AND has_column_privilege(r.rolname, c.oid, a.attnum, 'SELECT')"
    ).fetchall()
    leaks = sorted(
        (table, column, role)
        for table, column, role in rows
        if SECRET.search(str(column)) or (table, column) in NAMED_PERSONAL
    )
    assert leaks == []


def test_catalog_app_rw_no_platform_writes(db: Conn) -> None:
    for table in PLATFORM:
        row = db.execute(
            "SELECT has_table_privilege('app_rw', %(t)s, 'INSERT'), "
            "has_table_privilege('app_rw', %(t)s, 'UPDATE'), "
            "has_table_privilege('app_rw', %(t)s, 'DELETE'), "
            "has_table_privilege('app_rw', %(t)s, 'TRUNCATE'), "
            "has_any_column_privilege('app_rw', %(t)s, 'INSERT'), "
            "has_any_column_privilege('app_rw', %(t)s, 'UPDATE')",
            {"t": f"public.{table}"},
        ).fetchone()
        assert row == (False,) * 6, table
    # rules: brand rows only, through column INSERT and UPDATE (enabled); no DELETE
    row = db.execute(
        "SELECT has_table_privilege('app_rw', 'public.rules', 'INSERT'), "
        "has_table_privilege('app_rw', 'public.rules', 'UPDATE'), "
        "has_table_privilege('app_rw', 'public.rules', 'DELETE'), "
        "has_column_privilege('app_rw', 'public.rules', 'revision', 'UPDATE')"
    ).fetchone()
    assert row == (False, False, False, False)


def test_catalog_append_only_no_update_delete(db: Conn) -> None:
    for table in APPEND_ONLY:
        row = db.execute(
            "SELECT has_any_column_privilege('app_rw', %s, 'UPDATE'), "
            "has_table_privilege('app_rw', %s, 'DELETE'), "
            "has_table_privilege('app_rw', %s, 'TRUNCATE')",
            (f"public.{table}",) * 3,
        ).fetchone()
        assert row == (False, False, False), table
        triggers = {
            str(r[0])
            for r in db.execute(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal",
                (f"public.{table}",),
            ).fetchall()
        }
        assert {"append_only_guard", "append_only_truncate"} <= triggers, table


def test_catalog_helpers_granted(db: Conn) -> None:
    functions = {str(r[0]): r[1] for r in db.execute(OWN_FUNCTIONS).fetchall()}
    for helper in HELPERS:
        assert helper in functions, helper
        row = db.execute(
            "SELECT has_function_privilege('app_rw', %s, 'EXECUTE')", (functions[helper],)
        ).fetchone()
        assert row == (True,), helper


def test_catalog_no_execute_on_migrator_only(db: Conn) -> None:
    functions = {str(r[0]): r[1] for r in db.execute(OWN_FUNCTIONS).fetchall()}
    for name in MIGRATOR_ONLY:
        oid = functions[_signature(name)] if _signature(name) in functions else functions[name]
        row = db.execute(
            "SELECT has_function_privilege('app_rw', %s, 'EXECUTE'), "
            "has_function_privilege('app_ro', %s, 'EXECUTE'), "
            "has_function_privilege('app_platform_ro', %s, 'EXECUTE'), p.prosecdef, "
            "pg_get_userbyid(p.proowner) FROM pg_proc p WHERE p.oid = %s",
            (oid, oid, oid, oid),
        ).fetchone()
        assert row == (False, False, False, False, "migrator"), name


@pytest.mark.usefixtures("db_settings", "clean_tenant_db")
async def test_mkc01_surfaces_from_unit_of_work(superuser_conn: Conn) -> None:
    """A generator approving their own version, through S9's unit of work: the guard's MKC01
    surfaces as ConflictError(MAKER_CHECKER_VIOLATION) and nothing is committed."""
    from app.core.errors import ConflictError
    from app.db.session import unit_of_work
    from mkos_schemas.codes import ErrorCode
    from sqlalchemy import text

    tenant = f.tenant(superuser_conn)
    listing = f.listing(superuser_conn, tenant)
    owner = f.member(superuser_conn, tenant, role="owner")
    version = next_version(superuser_conn, tenant, listing, None, "generation", owner)
    with pytest.raises(ConflictError) as raised:
        async with unit_of_work(tenant) as session:
            await session.execute(
                text("UPDATE listing_versions SET approved_by_user_id = :u WHERE id = :v"),
                {"u": owner, "v": version},
            )
    assert raised.value.code is ErrorCode.MAKER_CHECKER_VIOLATION
    row = superuser_conn.execute(
        "SELECT approved_by_user_id FROM listing_versions WHERE id = %s", (version,)
    ).fetchone()
    assert row == (None,)
