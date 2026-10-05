"""0001 privileges: EXECUTE grants on the helpers (DATABASE.md §4 "Helper grants"), per-table and
per-column SELECT for the read-only roles (§8.1 "Read-only roles get no default SELECT")."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import sql

from . import factories as f
from .conftest import expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

READ_ONLY_ROLES = ["app_ro", "app_platform_ro"]
APP_ROLES = ["app_rw", *READ_ONLY_ROLES]
TABLES = [
    "tenants",
    "users",
    "tenant_memberships",
    "invitations",
    "business_parties",
    "brand_profiles",
    "products",
    "product_colourways",
    "variant_matrix_cells",
]
SECRET_COLUMNS = [
    ("users", "hashed_password"),
    ("invitations", "token_hash"),
    ("business_parties", "email"),
    ("business_parties", "phone"),
    ("business_parties", "address_line1"),
    ("business_parties", "address_line2"),
    ("business_parties", "postal_code"),
    ("business_parties", "gstin"),
    ("business_parties", "dedupe_key"),  # built from the contact columns above
]
GRANTED_FUNCTIONS = [
    "app_tenant_id()",
    "app_user_id()",
    "app_login_email()",
    "app_job_id()",
    "app_token_hash()",
    "fact_map_valid(jsonb, boolean)",
    "gtin_valid(text)",
]


def _set_role(conn: Conn, role: str) -> None:
    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))


def test_0001_privileges_app_rw_writes_full_graph(db: Conn) -> None:
    _set_role(db, "app_rw")
    tenant_id = f.tenant(db)
    owner = f.member(db, tenant_id)
    party = f.party(db, tenant_id)
    f.brand_profile(db, tenant_id, legal_entity_party_id=party, provenance={"x_y": f.fact()})
    style = f.product(db, tenant_id, "style", created_by=owner)
    colourway = f.colourway(db, tenant_id, style)
    child = f.product(
        db,
        tenant_id,
        "child",
        parent=style,
        manufacturer_party_id=party,
        identifier_type="ean",
        identifier_value="4006381333931",
        identifier_status="provided",
        attributes={"fabric": f.fact()},
    )
    f.cell(
        db,
        tenant_id,
        style,
        colourway_id=colourway,
        size_key="m",
        availability="available",
        unavailable_reason=None,
        variant_product_id=child,
    )
    row = db.execute("SELECT current_user, count(*) FROM products").fetchone()
    assert row == ("app_rw", 2)


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("role", READ_ONLY_ROLES)
def test_0001_privileges_read_only_select(db: Conn, role: str, table: str) -> None:
    f.tenant(db)
    _set_role(db, role)
    column = "tenant_id" if table == "brand_profiles" else "id"
    db.execute(sql.SQL("SELECT {} FROM {}").format(sql.Identifier(column), sql.Identifier(table)))


@pytest.mark.parametrize(("table", "column"), SECRET_COLUMNS)
@pytest.mark.parametrize("role", READ_ONLY_ROLES)
def test_0001_privileges_secret_columns_denied(
    db: Conn, role: str, table: str, column: str
) -> None:
    _set_role(db, role)
    expect_sqlstate(
        db,
        "42501",
        sql.SQL("SELECT {} FROM {}").format(sql.Identifier(column), sql.Identifier(table)),
    )
    expect_sqlstate(db, "42501", sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))


def test_0001_privileges_secret_columns_catalog(db: Conn) -> None:
    """No read-only role holds SELECT on a secret column, and every other column of the
    column-granted tables is readable (so a new column is a conscious grant decision)."""
    for role in READ_ONLY_ROLES:
        for table, column in SECRET_COLUMNS:
            row = db.execute(
                "SELECT has_column_privilege(%s, %s, %s, 'SELECT')", (role, table, column)
            ).fetchone()
            assert row == (False,), (role, table, column)
        for table in ("users", "invitations", "business_parties"):
            assert db.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')", (role, table)
            ).fetchone() == (False,)
            secret = {c for t, c in SECRET_COLUMNS if t == table}
            columns = db.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 "
                "AND NOT attisdropped",
                (table,),
            ).fetchall()
            for (name,) in columns:
                granted = db.execute(
                    "SELECT has_column_privilege(%s, %s, %s, 'SELECT')", (role, table, name)
                ).fetchone()
                assert granted == ((name not in secret),), (role, table, name)


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("role", READ_ONLY_ROLES)
def test_0001_privileges_app_ro_cannot_write(db: Conn, role: str, table: str) -> None:
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        row = db.execute(
            "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
        ).fetchone()
        assert row == (False,), (role, table, privilege)
    _set_role(db, role)
    expect_sqlstate(
        db,
        "42501",
        sql.SQL("DELETE FROM {}").format(sql.Identifier(table)),
    )


def _functions(conn: Conn) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT p.oid::regprocedure::text, p.oid::int FROM pg_proc p "
        "WHERE p.pronamespace = 'public'::regnamespace AND NOT EXISTS ("
        "SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass "
        "AND d.objid = p.oid AND d.deptype = 'e')"
    ).fetchall()
    return [(str(name), int(oid)) for name, oid in rows]


def test_0001_privileges_no_public_execute(db: Conn) -> None:
    functions = _functions(db)
    wanted = {name.replace(" ", "") for name in (*GRANTED_FUNCTIONS, "app_purge_tenant()")}
    assert {name for name, _ in functions} >= wanted
    for name, oid in functions:
        public = db.execute(
            "SELECT count(*) FROM pg_proc p, "
            "aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
            "WHERE p.oid = %s AND a.grantee = 0",
            (oid,),
        ).fetchone()
        assert public == (0,), name
        definer = db.execute("SELECT prosecdef FROM pg_proc WHERE oid = %s", (oid,)).fetchone()
        assert definer == (False,), name


@pytest.mark.parametrize("function", GRANTED_FUNCTIONS)
@pytest.mark.parametrize("role", APP_ROLES)
def test_0001_privileges_helpers_granted(db: Conn, role: str, function: str) -> None:
    row = db.execute(
        "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, f"public.{function}")
    ).fetchone()
    assert row == (True,)


@pytest.mark.parametrize("role", APP_ROLES)
def test_0001_privileges_purge_helper_ungranted(db: Conn, role: str) -> None:
    for function in ("public.app_purge_tenant()", "public.set_updated_at()"):
        row = db.execute(
            "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, function)
        ).fetchone()
        assert row == (False,), function
