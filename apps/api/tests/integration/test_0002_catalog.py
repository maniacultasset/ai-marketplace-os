"""Catalog-wide checks from revision 0002 on: tenant columns and their indexes, composite tenant
FKs (with the named exceptions), `updated_at` triggers, no ENUM types, `marketplace` CHECKs,
index and constraint names, and the 0002 round trip (DATABASE.md §1 conventions, §8.2)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.schema import Constraint

from .conftest import ScratchDb, catalog_snapshot

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

REVISION_0001 = "0001_core_tenancy"

# Nullable `tenant_id` by design: platform events (DATABASE.md §5.1 audit_log, KI-B) and platform
# rules (§5.2 rules). Neither has a UNIQUE index led by tenant_id; each keeps its documented one.
NULLABLE_TENANT = {
    "audit_log": ("tenant_id", "occurred_at", "id"),
    "rules": ("tenant_id", "marketplace"),
}
# The documented plain FK between tenant tables (DATABASE.md §5.2 rules, §5.7 listing_issues):
# a composite FK can't reach a platform rule (NULL tenant). The 0004 guard checks the tenant.
PLAIN_FK_EXCEPTIONS = {"fk_listing_issues_rule_id_rules"}


def _tenant_fks(conn: Conn) -> list[tuple[str, str, str, list[str], list[str]]]:
    """Every FK in `public` whose source and target tables both have a `tenant_id` column:
    (name, source table, target table, source columns, target columns), in key order."""
    rows = conn.execute("""
        SELECT con.conname, src.relname, dst.relname,
          ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, i)
                JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n ORDER BY k.i),
          ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, i)
                JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n ORDER BY k.i)
        FROM pg_constraint con
        JOIN pg_class src ON src.oid = con.conrelid
        JOIN pg_class dst ON dst.oid = con.confrelid
        WHERE con.contype = 'f' AND src.relnamespace = 'public'::regnamespace
          AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = con.conrelid
                      AND attname = 'tenant_id' AND NOT attisdropped)
          AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = con.confrelid
                      AND attname = 'tenant_id' AND NOT attisdropped)
        ORDER BY 1
        """).fetchall()
    return [(str(n), str(s), str(d), list(sc), list(dc)) for n, s, d, sc, dc in rows]


def tenant_fk_columns(conn: Conn, tables: set[str]) -> set[str]:
    """`table.column` for every non-tenant column of a tenant-composite FK out of `tables`: the
    columns a cross-tenant 23503 test must cover."""
    return {
        f"{source}.{column}"
        for _, source, _, columns, _ in _tenant_fks(conn)
        if source in tables and "tenant_id" in columns
        for column in columns
        if column != "tenant_id"
    }


def _tenant_columns(conn: Conn) -> list[tuple[str, bool]]:
    rows = conn.execute(
        "SELECT c.relname, a.attnotnull FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') "
        "AND a.attname = 'tenant_id' AND NOT a.attisdropped ORDER BY 1"
    ).fetchall()
    return [(str(r[0]), bool(r[1])) for r in rows]


def _indexes(conn: Conn, table: str) -> list[tuple[str, bool, list[str], str]]:
    """(name, unique, key columns in order ('' for an expression), definition) per index."""
    rows = conn.execute(
        "SELECT i.relname, x.indisunique, "
        "  ARRAY(SELECT coalesce(a.attname, '') FROM unnest(x.indkey) WITH ORDINALITY k(n, o) "
        "        LEFT JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.n "
        "        WHERE k.o <= x.indnkeyatts ORDER BY k.o), "
        "  pg_get_indexdef(x.indexrelid) "
        "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
        "WHERE x.indrelid = %s::regclass ORDER BY 1",
        (table,),
    ).fetchall()
    return [(str(n), bool(u), list(c), str(d)) for n, u, c, d in rows]


def test_tenant_columns_catalog(db: Conn) -> None:
    """Every `tenant_id` is NOT NULL, leads an index and leads a UNIQUE index (the composite-FK
    anchor), except the two nullable-by-design tables, which keep their documented index."""
    columns = _tenant_columns(db)
    for table, not_null in columns:
        indexes = _indexes(db, table)
        if table in NULLABLE_TENANT:
            assert not not_null, f"{table}.tenant_id is documented as nullable"
            wanted = list(NULLABLE_TENANT[table])
            assert any(cols == wanted for _, _, cols, _ in indexes), (table, indexes)
            continue
        assert not_null, f"{table}.tenant_id is nullable"
        assert any(cols[0] == "tenant_id" for _, _, cols, _ in indexes), table
        assert any(
            unique and cols[0] == "tenant_id" for _, unique, cols, _ in indexes
        ), f"{table}: no UNIQUE index led by tenant_id"


def _pinned(
    fks: list[tuple[str, str, str, list[str], list[str]]], source: str, column: str
) -> bool:
    """`column` of `source` sits in a tenant-composite FK, or is the row's own id."""
    if column == "id":
        return True
    return any(fk[1] == source and fk[3] == ["tenant_id", column] for fk in fks)


def test_tenant_fks_composite(db: Conn) -> None:
    """Every FK between two tenant tables carries `tenant_id` on both sides, paired. The named
    exceptions: the plain `listing_issues.rule_id` FK, and the companion FKs (`→ lv`, `→ l/mkt`,
    `(colourway_id, product_id)`, `(marketplace_account_id, marketplace)`, `(listing_item_id,
    listing_id)`, the 0001 same-style FK), each allowed only beside a tenant-composite FK on the
    same table that pins one of its columns (DATABASE.md §1)."""
    fks = _tenant_fks(db)
    for name, source, target, columns, target_columns in fks:
        if "tenant_id" in columns:
            paired = target_columns[columns.index("tenant_id")]
            assert paired == "tenant_id", f"{name}: tenant_id pairs with {target}.{paired}"
            continue
        if name in PLAIN_FK_EXCEPTIONS:
            continue
        assert len(columns) >= 2, f"{name}: {source}{columns} → {target} without tenant_id"
        assert any(_pinned(fks, source, column) for column in columns), (
            f"{name}: companion FK {source}{columns} → {target}{target_columns} has no column "
            "pinned by a tenant-composite FK"
        )


def test_updated_at_triggers_catalog(db: Conn) -> None:
    """Every table with `updated_at` has exactly one BEFORE UPDATE FOR EACH ROW
    `set_updated_at` trigger, and no table without the column has one."""
    rows = db.execute(
        "SELECT c.relname, "
        "  EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
        "          AND a.attname = 'updated_at' AND NOT a.attisdropped), "
        "  (SELECT count(*) FROM pg_trigger t WHERE t.tgrelid = c.oid AND NOT t.tgisinternal "
        "   AND t.tgfoid = 'public.set_updated_at()'::regprocedure AND (t.tgtype & 2) = 2 "
        "   AND (t.tgtype & 16) = 16 AND (t.tgtype & 1) = 1) "
        "FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' "
        "AND c.relname <> 'alembic_version'"
    ).fetchall()
    for table, has_column, triggers in rows:
        assert triggers == (1 if has_column else 0), (table, has_column, triggers)


def test_no_enum_types(db: Conn) -> None:
    rows = db.execute(
        "SELECT typname FROM pg_type WHERE typnamespace = 'public'::regnamespace "
        "AND typtype = 'e'"
    ).fetchall()
    assert rows == []


def test_marketplace_columns_checked(db: Conn) -> None:
    """Every `marketplace` column has a CHECK over exactly ('amazon', 'flipkart') (T9)."""
    tables = db.execute(
        "SELECT c.relname, a.attnum FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' "
        "AND a.attname = 'marketplace' AND NOT a.attisdropped ORDER BY 1"
    ).fetchall()
    for table, attnum in tables:
        checks = db.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass "
            "AND contype = 'c' AND conkey = ARRAY[%s]::smallint[]",
            (table, attnum),
        ).fetchall()
        wanted = "CHECK ((marketplace = ANY (ARRAY['amazon'::text, 'flipkart'::text])))"
        assert [str(c[0]) for c in checks] == [wanted], (table, checks)


def _metadata_names() -> list[tuple[str, str]]:
    from app.db.base import metadata

    names: list[tuple[str, str]] = []
    for table in metadata.tables.values():
        for index in table.indexes:
            names.append((table.name, str(index.name)))
        for constraint in table.constraints:
            if isinstance(constraint, (Constraint, UniqueConstraint, Index)) and constraint.name:
                names.append((table.name, str(constraint.name)))
    return names


def test_index_names_unique_short(db: Conn) -> None:
    """Every index and constraint name the models declare is at most 63 bytes, unique in the
    schema, and present under that exact name in the catalog (PostgreSQL would silently
    truncate a longer one, and the models would drift from the revision)."""
    names = _metadata_names()
    relation_names = [name for _, name in names]
    for table, name in names:
        assert len(name.encode()) <= 63, (table, name)
    in_catalog = {
        str(r[0])
        for r in db.execute(
            "SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
            "UNION SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND relkind = 'i'"
        ).fetchall()
    }
    missing = sorted(set(relation_names) - in_catalog)
    assert missing == []
    assert len(relation_names) == len(set(relation_names)), "a name is declared twice"


def _objects(db: ScratchDb) -> dict[str, list[tuple[object, ...]]]:
    with psycopg.connect(db.libpq, autocommit=True, connect_timeout=5) as conn:
        snapshot = catalog_snapshot(conn)
        triggers = conn.execute(
            "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal ORDER BY 1, 2"
        ).fetchall()
        constraints = conn.execute(
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
        ).fetchall()
    return {**snapshot, "triggers": triggers, "constraints": constraints}


def test_0002_downgrade_to_0001(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    """`downgrade 0001` from head leaves exactly the 0001 catalog (relations, sequences,
    functions, triggers, constraints), and the next `upgrade head` succeeds."""
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION_0001)
    at_0001 = _objects(scratch_db)
    command.upgrade(cfg, "head")
    at_head = _objects(scratch_db)
    command.downgrade(cfg, REVISION_0001)
    assert _objects(scratch_db) == at_0001
    command.upgrade(cfg, "head")
    assert _objects(scratch_db) == at_head
