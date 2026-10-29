"""0004/15_privileges and the per-table grants of the earlier 0004 files (DATABASE.md Privileges
bullets :229, :244, :438, :453, :789, :848, :881, :884, :1134, :1150, :1328, :1355; §8.1): what
`app_rw` holds on each table, written here as a literal."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

RW = frozenset({"SELECT", "INSERT", "UPDATE"})
APPEND = frozenset({"SELECT", "INSERT"})
READ = frozenset({"SELECT"})
OUTCOME = frozenset(
    {
        "outcome",
        "error_code",
        "error_report_json",
        "submission_ref",
        "raw_response_sha256",
        "raw_response_storage_key",
        "sent_at",
        "responded_at",
        "resolved_at",
    }
)
RULE_COLUMNS = frozenset(
    {
        "tenant_id",
        "marketplace",
        "category",
        "field",
        "rule_type",
        "subject",
        "pack",
        "severity",
        "version",
        "enabled",
        "rule_def",
    }
)
VERSION_INSERT = frozenset(
    {
        "id",
        "tenant_id",
        "listing_id",
        "version",
        "content_json",
        "image_reports_json",
        "generated_by_user_id",
        "edited_by_user_id",
        "origin",
        "parent_version_id",
        "changed_paths",
        "locale",
        "rules_version",
        "category_template_id",
        "job_id",
        "attempts_used",
        "created_at",
    }
)
# table → (table-level privileges, column-level UPDATE, column-level INSERT)
MATRIX: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {
    "tenants": (APPEND, frozenset({"name", "tone", "guidelines", "glossary"}), frozenset()),
    "users": (RW, frozenset(), frozenset()),
    "tenant_memberships": (
        APPEND,
        frozenset({"role", "is_active", "deactivated_at", "last_used_at"}),
        frozenset(),
    ),
    "product_colourways": (RW, frozenset(), frozenset()),
    "variant_matrix_cells": (RW, frozenset(), frozenset()),
    "brand_profiles": (RW, frozenset(), frozenset()),
    "business_parties": (RW, frozenset(), frozenset()),
    "listing_versions": (READ, frozenset({"approved_by_user_id", "approved_at"}), VERSION_INSERT),
    "listing_items": (RW, frozenset(), frozenset()),
    # 0011b: a reconnect moves a listing's submissions to the new account (ls_guard checks the move)
    "listing_submissions": (APPEND, OUTCOME | {"marketplace_account_id"}, frozenset()),
    "publish_field_results": (APPEND, frozenset(), frozenset()),
    "listing_issues": (RW, frozenset(), frozenset()),
    "human_tasks": (RW, frozenset(), frozenset()),
    "audit_log": (APPEND, frozenset(), frozenset()),
    "agent_runs": (APPEND, frozenset(), frozenset()),
    "validation_runs": (APPEND, frozenset(), frozenset()),
    "review_decisions": (APPEND, frozenset(), frozenset()),
    "rules": (READ, frozenset({"enabled"}), RULE_COLUMNS),
    "category_templates": (READ, frozenset(), frozenset()),
    "marketplace_error_map": (READ, frozenset(), frozenset()),
    "attribute_definitions": (READ, frozenset(), frozenset()),
    "attribute_columns": (READ, frozenset(), frozenset()),
    "category_attributes": (READ, frozenset(), frozenset()),
    "listing_status_transitions": (READ, frozenset(), frozenset()),
}


def _table_privileges(db: Conn, table: str) -> frozenset[str]:
    rows = db.execute(
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema = 'public' AND table_name = %s AND grantee = 'app_rw'",
        (table,),
    ).fetchall()
    return frozenset(str(r[0]) for r in rows)


def _column_privileges(db: Conn, table: str, privilege: str) -> frozenset[str]:
    """Columns granted `privilege` one by one (column_privileges also lists table-level grants,
    so a table-level grant is subtracted by the caller's matrix)."""
    rows = db.execute(
        "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 "
        "AND NOT a.attisdropped AND a.attacl IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM aclexplode(a.attacl) x WHERE x.grantee = 'app_rw'::regrole "
        "AND x.privilege_type = %s)",
        (f"public.{table}", privilege),
    ).fetchall()
    return frozenset(str(r[0]) for r in rows)


@pytest.mark.parametrize("table", sorted(MATRIX))
def test_privilege_matrix(db: Conn, table: str) -> None:
    table_level, column_update, column_insert = MATRIX[table]
    assert _table_privileges(db, table) == table_level
    assert _column_privileges(db, table, "UPDATE") == column_update
    assert _column_privileges(db, table, "INSERT") == column_insert


def test_app_rw_no_truncate_anywhere(db: Conn) -> None:
    rows = db.execute(
        "SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
        "AND c.relkind IN ('r', 'p') AND has_table_privilege('app_rw', c.oid, 'TRUNCATE')"
    ).fetchall()
    assert rows == []


def test_app_rw_cannot_delete_tenants(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        expect_sqlstate(db, "42501", "DELETE FROM tenants WHERE id = %s", (tenant,))
        expect_sqlstate(db, "42501", "TRUNCATE tenants")


def test_tenants_update_only_profile_columns(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db, tenant):
        updated = db.execute(
            "UPDATE tenants SET name = 'Renamed', tone = 'warm', guidelines = '[]', glossary = '{}' "
            "WHERE id = %s",
            (tenant,),
        )
        assert updated.rowcount == 1
        for assignment in (
            "status = 'suspended'",
            "slug = 'x-y'",
            "auto_publish = false",
            "purge_after = now()",
        ):
            expect_sqlstate(
                db,
                "42501",
                f"UPDATE tenants SET {assignment} WHERE id = %s",  # noqa: S608
                (tenant,),
            )


def test_memberships_update_columns_only(db: Conn) -> None:
    tenant = f.tenant(db)
    f.member(db, tenant)
    user = f.member(db, tenant, role="operator")
    with as_app_rw(db, tenant):
        updated = db.execute(
            "UPDATE tenant_memberships SET role = 'reviewer', last_used_at = now() "
            "WHERE tenant_id = %s AND user_id = %s",
            (tenant, user),
        )
        assert updated.rowcount == 1
        expect_sqlstate(
            db,
            "42501",
            "UPDATE tenant_memberships SET tenant_id = tenant_id WHERE user_id = %s",
            (user,),
        )
        expect_sqlstate(db, "42501", "DELETE FROM tenant_memberships WHERE user_id = %s", (user,))
