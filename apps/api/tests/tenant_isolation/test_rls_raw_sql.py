"""Layer 3 alone (DATABASE.md:129; S10 DW6): raw SQL as the `app_rw` login, with no repository and
no ORM guard in the way. Under tenant A's GUC no statement reads, inserts, updates or deletes a
row of tenant B on any RLS table; with no GUC every tenant table reads as empty; a hand-stamped
`SCOPE_OK` reaches RLS and still gets 0 rows; and the platform role reads every tenant."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy import select

from .factories import World, build_world

Conn = psycopg.Connection[tuple[Any, ...]]

# Every table 0005 puts under RLS, with the column that names its tenant. A catalog test below
# keeps this list equal to the database's, so a new tenant table cannot be missed here.
TENANT_COLUMN: dict[str, str] = {
    "agent_runs": "tenant_id",
    "audit_log": "tenant_id",
    "batches": "tenant_id",
    "brand_chunks": "tenant_id",
    "brand_documents": "tenant_id",
    "brand_profiles": "tenant_id",
    "business_parties": "tenant_id",
    "human_tasks": "tenant_id",
    "idempotency_keys": "tenant_id",
    "images": "tenant_id",
    "import_rows": "tenant_id",
    "imports": "tenant_id",
    "invitations": "tenant_id",
    "job_runs": "tenant_id",
    "listing_issues": "tenant_id",
    "listing_items": "tenant_id",
    "listing_submissions": "tenant_id",
    "listing_versions": "tenant_id",
    "listings": "tenant_id",
    "marketplace_accounts": "tenant_id",
    "oauth_states": "tenant_id",
    "performance": "tenant_id",
    "product_colourways": "tenant_id",
    "products": "tenant_id",
    "publish_field_results": "tenant_id",
    "refresh_tokens": "tenant_id",
    "review_decisions": "tenant_id",
    "rules": "tenant_id",
    "tenant_memberships": "tenant_id",
    "tenants": "id",
    "validation_runs": "tenant_id",
    "variant_matrix_cells": "tenant_id",
}
TABLES = sorted(TENANT_COLUMN)


@pytest.fixture(scope="module")
def world(clean_module_admin_conn: Conn) -> World:
    """Built once: every test here only reads, or fails a write and rolls back."""
    return build_world(clean_module_admin_conn, 2)


@contextmanager
def as_tenant(conn: Conn, tenant: UUID | None) -> Iterator[Conn]:
    """One transaction on the app_rw login, with `app.tenant_id` set when `tenant` is given,
    always rolled back."""
    with conn.transaction(force_rollback=True):
        if tenant is not None:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant)])
        yield conn


def _count(conn: Conn, table: str, tenant: UUID | None = None) -> int:
    query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
    params: list[Any] = []
    if tenant is not None:
        query = query + sql.SQL(" WHERE {} = %s").format(sql.Identifier(TENANT_COLUMN[table]))
        params = [tenant]
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return int(str(row[0]))


def _granted_columns(conn: Conn, table: str, privilege: str | None) -> list[str]:
    """The table's writable columns (no generated or identity column), only those app_rw holds
    `privilege` on unless it is None."""
    rows = conn.execute(
        "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 "
        "AND NOT a.attisdropped AND a.attgenerated = '' AND a.attidentity = '' "
        "AND (%s::text IS NULL OR has_column_privilege('app_rw', a.attrelid, a.attnum, %s)) "
        "ORDER BY a.attnum",
        [f"public.{table}", privilege, privilege or "SELECT"],
    ).fetchall()
    return [str(r[0]) for r in rows]


def _has(conn: Conn, table: str, privilege: str) -> bool:
    row = conn.execute(
        "SELECT has_table_privilege('app_rw', %s, %s)", [f"public.{table}", privilege]
    ).fetchone()
    return bool(row and row[0])


def test_rls_table_list_matches_catalog(admin_conn: Conn) -> None:
    rows = admin_conn.execute(
        "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
        "AND relkind IN ('r', 'p') AND relrowsecurity"
    ).fetchall()
    assert {str(r[0]) for r in rows} == set(TENANT_COLUMN) | {"users"}


@pytest.mark.parametrize("table", TABLES)
def test_rls_raw_sql_cross_tenant(
    admin_conn: Conn, app_conn: Conn, world: World, table: str
) -> None:
    a, b = world.tenants
    assert _count(admin_conn, table, b) >= 1, f"the world has no {table} row of tenant B"
    with as_tenant(app_conn, a):
        assert _count(app_conn, table, b) == 0
        assert _count(app_conn, table, a) == _count(admin_conn, table, a) >= 1


def test_rls_raw_sql_users_cross_tenant(admin_conn: Conn, app_conn: Conn, world: World) -> None:
    a, b = world.tenants
    members = "SELECT user_id FROM tenant_memberships WHERE tenant_id = %s"
    of_a = {r[0] for r in admin_conn.execute(members, [a]).fetchall()}
    of_b = {r[0] for r in admin_conn.execute(members, [b]).fetchall()}
    assert of_a and of_b and not of_a & of_b
    with as_tenant(app_conn, a):
        seen = {r[0] for r in app_conn.execute("SELECT id FROM users").fetchall()}
    assert seen == of_a


@pytest.mark.parametrize("table", [*TABLES, "users"])
def test_rls_no_guc_zero_rows(app_conn: Conn, world: World, table: str) -> None:
    """A forgotten GUC reads as an empty table, never as every tenant (DATABASE.md:131). Only
    the platform rows of `rules` stay readable."""
    assert world.tenants
    with as_tenant(app_conn, None):
        if table == "rules":
            row = app_conn.execute("SELECT count(*) FROM rules WHERE tenant_id IS NOT NULL")
            assert row.fetchone() == (0,)
            assert _count(app_conn, "rules") >= 1
        else:
            assert _count(app_conn, table) == 0


@pytest.mark.parametrize("table", TABLES)
def test_rls_insert_other_tenant_refused(
    admin_conn: Conn, app_conn: Conn, world: World, table: str
) -> None:
    """A copy of B's row, inserted under A's GUC, fails 42501: the RLS WITH CHECK when app_rw may
    insert the table, the privilege layer when it may not."""
    a, b = world.tenants
    column = TENANT_COLUMN[table]
    found = admin_conn.execute(
        sql.SQL("SELECT to_jsonb(t) FROM {} t WHERE {} = %s LIMIT 1").format(
            sql.Identifier(table), sql.Identifier(column)
        ),
        [b],
    ).fetchone()
    assert found is not None
    record: dict[str, Any] = dict(found[0])
    if "id" in record:
        record["id"] = str(uuid4())  # a new row, never an update of B's
    columns = _granted_columns(admin_conn, table, "INSERT") or _granted_columns(
        admin_conn, table, None
    )
    before = _count(admin_conn, table, b)
    statement = sql.SQL(
        "INSERT INTO {t} ({cols}) SELECT {cols} FROM jsonb_populate_record(NULL::{t}, %s)"
    ).format(t=sql.Identifier(table), cols=sql.SQL(", ").join(map(sql.Identifier, columns)))
    with as_tenant(app_conn, a), pytest.raises(psycopg.errors.InsufficientPrivilege) as info:
        app_conn.execute(statement, [Jsonb(record)])
    if _has(admin_conn, table, "INSERT"):
        assert "row-level security" in str(info.value), info.value
    assert _count(admin_conn, table, b) == before


@pytest.mark.parametrize("table", TABLES)
def test_rls_update_other_tenant_zero(
    admin_conn: Conn, app_conn: Conn, world: World, table: str
) -> None:
    """UPDATE (a no-op on a column app_rw may update) and DELETE of B's rows under A's GUC touch
    0 rows; a table without the privilege refuses with 42501 first."""
    a, b = world.tenants
    column = TENANT_COLUMN[table]
    where = sql.SQL("{} = %s").format(sql.Identifier(column))
    updatable = _granted_columns(admin_conn, table, "UPDATE")
    with as_tenant(app_conn, a):
        if updatable:
            target = sql.Identifier(updatable[0])
            update = sql.SQL("UPDATE {} SET {} = {} WHERE ").format(
                sql.Identifier(table), target, target
            )
            assert app_conn.execute(update + where, [b]).rowcount == 0
        else:
            with pytest.raises(psycopg.errors.InsufficientPrivilege), app_conn.transaction():
                app_conn.execute(
                    sql.SQL("UPDATE {} SET {} = {} WHERE ").format(
                        sql.Identifier(table), sql.Identifier(column), sql.Identifier(column)
                    )
                    + where,
                    [b],
                )
        if _has(admin_conn, table, "DELETE"):
            delete = sql.SQL("DELETE FROM {} WHERE ").format(sql.Identifier(table))
            assert app_conn.execute(delete + where, [b]).rowcount == 0
    assert _count(admin_conn, table, b) >= 1


@pytest.mark.usefixtures("iso_settings")
async def test_rls_hand_stamped_scope_ok_zero_rows(world: World) -> None:
    """A statement that carries SCOPE_OK by hand passes the ORM guard (layers 1-2) but not RLS: in
    a session with no tenant GUC it reads nothing (DATABASE.md:129)."""
    from app.db.guard import SCOPE_OK
    from app.db.models.catalog import Product
    from app.db.session import get_sessionmaker

    stmt = select(Product.id).execution_options(**{SCOPE_OK: True})
    async with get_sessionmaker()() as session:
        try:
            assert (await session.execute(stmt)).scalars().all() == []
        finally:
            await session.rollback()
    assert world.rows["products"]


@pytest.mark.parametrize("table", TABLES)
def test_rls_platform_ro_reads_all(
    admin_conn: Conn, app_conn: Conn, world: World, table: str
) -> None:
    """`app_platform_ro` (SET ROLE from app_rw, as platform_scope does) reads every tenant's rows
    of each table it holds a grant on; plain app_rw never does (t_platform's current_user guard)."""
    column = TENANT_COLUMN[table]
    readable = admin_conn.execute(
        "SELECT has_column_privilege('app_platform_ro', %s, %s, 'SELECT')",
        [f"public.{table}", column],
    ).fetchone()
    tenants_of = sql.SQL("SELECT DISTINCT {} FROM {} WHERE {} IS NOT NULL").format(
        sql.Identifier(column), sql.Identifier(table), sql.Identifier(column)
    )
    with app_conn.transaction(force_rollback=True):
        app_conn.execute("SET LOCAL ROLE app_platform_ro")
        if not (readable and readable[0]):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), app_conn.transaction():
                app_conn.execute(tenants_of)
            return
        seen = {r[0] for r in app_conn.execute(tenants_of).fetchall()}
    assert seen == set(world.tenants)
