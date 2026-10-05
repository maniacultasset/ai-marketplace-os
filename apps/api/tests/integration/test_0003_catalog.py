"""Catalog checks over the 14 tables of revision 0003_ops_tables (DATABASE.md §1 conventions, §8.1
read-only roles, §8.2 ledger row 0003 and catalog assertions): digest CHECKs schema-wide, the std
tenant shape, the platform tables, the read-only grants that never reach a secret column, no ENUM
vocabularies, and tenant-composite FKs with the six bound companion links."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

TENANT_TABLES = (
    "job_runs",
    "imports",
    "import_rows",
    "idempotency_keys",
    "refresh_tokens",
    "oauth_states",
    "listing_submissions",
    "publish_field_results",
)
PLATFORM_TABLES = (
    "category_templates",
    "marketplace_error_map",
    "attribute_definitions",
    "attribute_columns",
    "category_attributes",
)
TABLES_0003 = (*TENANT_TABLES, *PLATFORM_TABLES, "rate_limit_counters")
READ_ONLY_ROLES = ("app_ro", "app_platform_ro")
# DATABASE.md:1750 and :1787: hashes, the PKCE verifier and the uploaded CSV cells.
SECRET_COLUMNS = frozenset(
    {
        ("refresh_tokens", "token_hash"),
        ("oauth_states", "state_hash"),
        ("oauth_states", "code_verifier"),
        ("idempotency_keys", "request_hash"),
        ("import_rows", "raw_row"),
    }
)
SECRET_PATTERN = "(token|hash|password|secret|verifier|_enc)$"
DIGEST_NAME = "_sha256$|_digest$|^token_hash$|^state_hash$|^request_hash$"
# AC 7: companion FKs without tenant_id, each bound by a tenant-composite FK on the same table
# (name → the binding FK).
COMPANION_FKS = {
    "fk_listing_submissions_listing_version_id": "fk_listing_submissions_listing_id",
    "fk_listing_submissions_listing_id_marketplace": "fk_listing_submissions_listing_id",
    "fk_listing_submissions_marketplace_account_id_marketplace": (
        "fk_listing_submissions_marketplace_account_id"
    ),
    "fk_listing_submissions_previous_submission_id": "fk_listing_submissions_listing_id",
    "fk_publish_field_results_submission_id_listing_id": "fk_publish_field_results_submission_id",
    "fk_publish_field_results_listing_item_id": "fk_publish_field_results_submission_id_listing_id",
}


def _columns(db: Conn, table: str) -> list[str]:
    rows = db.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 "
        "AND NOT attisdropped ORDER BY attnum",
        (table,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def test_ops_constraints_every_digest_column_checked(db: Conn) -> None:
    rows = db.execute(
        "SELECT c.relname, a.attname, a.attnum FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid WHERE c.relnamespace = 'public'::regnamespace "
        "AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped "
        "AND a.atttypid = 'bytea'::regtype AND a.attname ~ %s ORDER BY 1, 2",
        (DIGEST_NAME,),
    ).fetchall()
    found = {(str(t), str(c)) for t, c, _ in rows}
    # the 0003 digests are all in the scan
    assert {
        ("refresh_tokens", "token_hash"),
        ("oauth_states", "state_hash"),
        ("idempotency_keys", "request_hash"),
        ("category_templates", "source_digest"),
        ("listing_submissions", "payload_sha256"),
        ("listing_submissions", "facts_digest"),
        ("listing_submissions", "raw_response_sha256"),
    } <= found
    for table, column, attnum in rows:
        checks = db.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass "
            "AND contype = 'c' AND %s = ANY (conkey)",
            (table, attnum),
        ).fetchall()
        wanted = f"octet_length({column}) = 32"
        assert any(wanted in str(c[0]) for c in checks), (table, column, checks)


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_ops_constraints_std_tenant(db: Conn, table: str) -> None:
    not_null = db.execute(
        "SELECT attnotnull FROM pg_attribute WHERE attrelid = %s::regclass "
        "AND attname = 'tenant_id'",
        (table,),
    ).fetchone()
    assert not_null == (True,), table
    cascade = db.execute(
        "SELECT conname, confdeltype FROM pg_constraint WHERE conrelid = %s::regclass "
        "AND contype = 'f' AND confrelid = 'tenants'::regclass",
        (table,),
    ).fetchall()
    assert cascade == [(f"fk_{table}_tenant_id_tenants", "c")], table
    if "id" in _columns(db, table):
        anchor = db.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'u' "
            "AND conkey = ARRAY(SELECT attnum FROM pg_attribute WHERE attrelid = %s::regclass "
            "AND attname IN ('tenant_id', 'id') ORDER BY attname = 'id')::smallint[]",
            (table, table),
        ).fetchall()
        assert anchor == [(f"uq_{table}_tenant_id_id",)], table
    else:
        assert table == "idempotency_keys"


def test_platform_tables_have_no_tenant(db: Conn) -> None:
    for table in (*PLATFORM_TABLES, "rate_limit_counters"):
        assert "tenant_id" not in _columns(db, table), table


@pytest.mark.parametrize("role", READ_ONLY_ROLES)
@pytest.mark.parametrize("table", TABLES_0003)
def test_readonly_roles_select_ops_tables(db: Conn, role: str, table: str) -> None:
    secret = {column for t, column in SECRET_COLUMNS if t == table}
    table_level = db.execute(
        "SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"public.{table}")
    ).fetchone()
    assert table_level == ((not secret),), (role, table)
    for column in _columns(db, table):
        granted = db.execute(
            "SELECT has_column_privilege(%s, %s, %s, 'SELECT')", (role, f"public.{table}", column)
        ).fetchone()
        assert granted == ((column not in secret),), (role, table, column)
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        write = db.execute(
            "SELECT has_table_privilege(%s, %s, %s)", (role, f"public.{table}", privilege)
        ).fetchone()
        assert write == (False,), (role, table, privilege)


def test_readonly_roles_no_secret_columns(db: Conn) -> None:
    hits = db.execute(
        "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE c.relnamespace = 'public'::regnamespace AND a.attnum > 0 AND NOT a.attisdropped "
        "AND a.attname ~ %s AND c.relname = ANY (%s)",
        (SECRET_PATTERN, list(TABLES_0003)),
    ).fetchall()
    pattern_hits = {(str(t), str(c)) for t, c in hits}
    assert pattern_hits <= SECRET_COLUMNS
    for role in READ_ONLY_ROLES:
        for table, column in SECRET_COLUMNS | pattern_hits:
            granted = db.execute(
                "SELECT has_column_privilege(%s, %s, %s, 'SELECT')",
                (role, f"public.{table}", column),
            ).fetchone()
            assert granted == (False,), (role, table, column)


@pytest.mark.parametrize("role", READ_ONLY_ROLES)
def test_readonly_roles_cannot_read_secrets(db: Conn, role: str) -> None:
    db.execute(f"SET LOCAL ROLE {role}")
    for table, column in sorted(SECRET_COLUMNS):
        try:
            with db.transaction():
                db.execute(f"SELECT {column} FROM public.{table}")  # noqa: S608 - fixed names
        except psycopg.Error as error:
            assert error.sqlstate == "42501", (table, column, error)
        else:
            raise AssertionError(f"{role} read {table}.{column}")
    db.execute("SELECT id, tenant_id, user_id, expires_at FROM public.oauth_states")


def test_no_vocab_enum_types(db: Conn) -> None:
    rows = db.execute(
        "SELECT typname FROM pg_type WHERE typnamespace = 'public'::regnamespace "
        "AND typtype = 'e' AND typname NOT LIKE 'procrastinate\\_%'"
    ).fetchall()
    assert rows == []


def _fks(db: Conn) -> list[tuple[str, str, str, list[str], list[str]]]:
    rows = db.execute(
        """
        SELECT con.conname, src.relname, dst.relname,
          ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, i)
                JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n ORDER BY k.i),
          ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, i)
                JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n ORDER BY k.i)
        FROM pg_constraint con
        JOIN pg_class src ON src.oid = con.conrelid
        JOIN pg_class dst ON dst.oid = con.confrelid
        WHERE con.contype = 'f' AND src.relname = ANY (%s)
          AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = con.confrelid
                      AND attname = 'tenant_id' AND NOT attisdropped)
        ORDER BY 1
        """,
        (list(TABLES_0003),),
    ).fetchall()
    return [(str(n), str(s), str(d), list(sc), list(dc)) for n, s, d, sc, dc in rows]


def test_ops_tenant_fks_composite(db: Conn) -> None:
    fks = _fks(db)
    by_name = {fk[0]: fk for fk in fks}
    seen_companions: set[str] = set()
    for name, source, target, columns, target_columns in fks:
        if target == "tenants":
            assert columns == ["tenant_id"] and target_columns == ["id"], name
            continue
        if "tenant_id" in columns:
            assert columns[0] == "tenant_id" and target_columns[0] == "tenant_id", name
            continue
        assert name in COMPANION_FKS, f"{name}: {source}{columns} → {target} without tenant_id"
        seen_companions.add(name)
        binding = by_name[COMPANION_FKS[name]]
        assert binding[1] == source, name
        assert set(binding[3]) & set(columns), f"{name}: its binding FK shares no column"
        assert "tenant_id" in binding[3] or binding[0] in COMPANION_FKS, name
    assert seen_companions == set(COMPANION_FKS)
    # Decision 8: the requeue parent stays in its tenant
    parent = by_name["fk_job_runs_parent_job_id"]
    assert parent[3:] == (["tenant_id", "parent_job_id"], ["tenant_id", "id"])
