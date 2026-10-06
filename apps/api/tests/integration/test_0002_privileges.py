"""0002 privileges: per-table and per-column SELECT for the read-only roles (DATABASE.md §8.1
"Read-only roles get no default SELECT"; the §8.2 catalog assertion on secret-looking columns),
and app_rw writing the new tables through the 0000 default privileges (its REVOKEs are 0004)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import sql

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import rls_member, rls_tenant
from .test_0002_catalog import TABLES_0002

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

READ_ONLY_ROLES = ["app_ro", "app_platform_ro"]
# Never readable by the read-only roles: the KMS-wrapped token (invariant 6); task answers,
# suggestions and evidence (may hold contact data, §8.1); the audit actor's email and IP
# (personal data, redacted with the actor); and every column the §8.2 catalog assertion
# `(token|hash|password|secret|verifier|_enc)$` matches.
SECRET_COLUMNS = [
    ("marketplace_accounts", "token_enc"),
    ("human_tasks", "answer"),
    ("human_tasks", "suggestions"),
    ("human_tasks", "evidence"),
    ("audit_log", "actor_email"),
    ("audit_log", "ip"),
    ("images", "content_hash"),
    ("brand_documents", "content_hash"),
    ("brand_chunks", "content_hash"),
]
COLUMN_GRANTED = sorted({table for table, _ in SECRET_COLUMNS})


def _set_role(conn: Conn, role: str) -> None:
    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))


@pytest.mark.parametrize("table", sorted(TABLES_0002))
@pytest.mark.parametrize("role", READ_ONLY_ROLES)
def test_0002_privileges_read_only_select(db: Conn, role: str, table: str) -> None:
    _set_role(db, role)
    db.execute(sql.SQL("SELECT id FROM {}").format(sql.Identifier(table)))


@pytest.mark.parametrize(("table", "column"), SECRET_COLUMNS)
@pytest.mark.parametrize("role", READ_ONLY_ROLES)
def test_0002_privileges_secret_columns_denied(
    db: Conn, role: str, table: str, column: str
) -> None:
    _set_role(db, role)
    expect_sqlstate(
        db,
        "42501",
        sql.SQL("SELECT {} FROM {}").format(sql.Identifier(column), sql.Identifier(table)),
    )
    expect_sqlstate(db, "42501", sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))


def test_0002_privileges_secret_columns_catalog(db: Conn) -> None:
    """No read-only role holds SELECT on a secret column of a 0002 table or on any column the
    §8.2 pattern matches; every other column of the column-granted tables is readable, so a new
    column is a conscious grant decision."""
    pattern_hits = db.execute(
        "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND a.attnum > 0 "
        "AND NOT a.attisdropped AND a.attname ~ '(token|hash|password|secret|verifier|_enc)$' "
        "AND c.relname = ANY (%s)",
        (sorted(TABLES_0002),),
    ).fetchall()
    assert {(str(t), str(c)) for t, c in pattern_hits} <= set(SECRET_COLUMNS)
    for role in READ_ONLY_ROLES:
        for table in COLUMN_GRANTED:
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


@pytest.mark.parametrize("table", sorted(TABLES_0002))
def test_0002_privileges_read_only_cannot_write(db: Conn, table: str) -> None:
    for role in READ_ONLY_ROLES:
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            row = db.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
            ).fetchone()
            assert row == (False,), (role, table, privilege)


def test_0002_privileges_app_rw_writes_full_graph(db: Conn) -> None:
    """The 0000 default privileges cover the new tables and `rules_revision_seq` (the INSERT
    default calls nextval)."""
    _set_role(db, "app_rw")
    tenant_id = rls_tenant(db)  # 0005: the tenant GUC comes first under RLS
    owner = rls_member(db, tenant_id)
    f.rule(db, tenant_id)
    product_id = f.product(db, tenant_id)
    listing_id = f.listing(db, tenant_id, product_id)
    version_id = f.version(
        db, tenant_id, listing_id, generated_by_user_id=rls_member(db, tenant_id, "operator")
    )
    f.review_decision(
        db,
        tenant_id,
        listing_id,
        version_id,
        decided_by_user_id=rls_member(db, tenant_id, "reviewer"),
    )
    f.listing_item(db, tenant_id, listing_id, product_id)
    f.validation_run(db, tenant_id, listing_id)
    f.agent_run(db, tenant_id, listing_id=listing_id)
    f.performance(db, tenant_id, listing_id)
    f.audit(db, tenant_id)
    f.brand_document(db, tenant_id, created_by=owner)
    f.brand_chunk(db, tenant_id)
    f.image(db, tenant_id, product_id)
    f.human_task(db, tenant_id, product_id=product_id)
    f.listing_issue(db, tenant_id, listing_id)
    f.batch(db, tenant_id, created_by_user_id=owner)
    f.marketplace_account(db, tenant_id, connected_by_user_id=owner)
    row = db.execute("SELECT current_user, count(*) FROM listings").fetchone()
    assert row == ("app_rw", 1)
