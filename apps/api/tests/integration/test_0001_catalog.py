"""Catalog-wide checks over revision 0001: `updated_at` triggers, the tenant purge shape, no
drift at head, composite tenant FKs (and a 23503 test for every FK column), the round trip and
an atomic failed upgrade (DATABASE.md §1 conventions, §8.2)."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql

from . import factories as f
from . import (
    test_0001_memberships,
    test_0001_parties,
    test_0001_products_facts,
    test_0001_products_family,
    test_0001_variants,
)
from .conftest import ScratchDb, catalog_snapshot, expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

TABLES_0001 = [
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
FUNCTIONS_0001 = [
    "set_updated_at()",
    "app_tenant_id()",
    "app_user_id()",
    "app_job_id()",
    "app_purge_tenant()",
    "app_login_email()",
    "app_token_hash()",
    "gtin_valid(text)",
    "fact_map_valid(jsonb,boolean)",
]
# The one FK between tenant tables without `tenant_id` in its key: the same-style FK, safe only
# while the same table also pins its style with a composite `(tenant_id, style_product_id)` FK.
SAME_STYLE_FK = "fk_variant_matrix_cells_colourway_id_style_product_id"


def _graph(conn: Conn, tenant_id: UUID) -> None:
    """One of everything in a tenant: members, an invitation, parties, the brand profile, a
    style with a colourway, a child and its cell, and a standalone product."""
    owner = f.member(conn, tenant_id)
    f.member(conn, tenant_id, role="reviewer", invited_by_user_id=owner)
    f.insert(
        conn,
        "invitations",
        tenant_id=tenant_id,
        email=f"invitee-{tenant_id}@x.test",
        role="operator",
        token_hash=bytes(16) + tenant_id.bytes,
        expires_at="2099-01-01",
        invited_by_user_id=owner,
    )
    party = f.party(conn, tenant_id)
    care = f.party(
        conn, tenant_id, legal_name=None, roles=["consumer_care"], phone="+91 98765 43210"
    )
    f.brand_profile(conn, tenant_id, legal_entity_party_id=party, consumer_care_party_id=care)
    style = f.product(conn, tenant_id, "style", created_by=owner, manufacturer_party_id=party)
    colourway = f.colourway(conn, tenant_id, style)
    child = f.product(conn, tenant_id, "child", parent=style, packer_party_id=party)
    f.cell(
        conn,
        tenant_id,
        style,
        colourway_id=colourway,
        size_key="m",
        availability="available",
        unavailable_reason=None,
        variant_product_id=child,
    )
    f.cell(conn, tenant_id, style, colourway_id=colourway, size_key="l")
    f.product(conn, tenant_id, importer_party_id=party, consumer_care_party_id=care)


def _counts(conn: Conn, tenant_id: UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in TABLES_0001:
        if table in ("tenants", "users"):
            continue
        row = conn.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(sql.Identifier(table)),
            (tenant_id,),
        ).fetchone()
        assert row is not None
        counts[table] = int(row[0])
    row = conn.execute("SELECT count(*) FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
    assert row is not None
    counts["tenants"] = int(row[0])
    return counts


def test_tenant_delete_cascades(db: Conn) -> None:
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    _graph(db, tenant_a)
    _graph(db, tenant_b)
    before_b = _counts(db, tenant_b)
    assert all(count > 0 for count in _counts(db, tenant_a).values())
    users_before = db.execute("SELECT count(*) FROM users").fetchone()
    # NO ACTION by design: the purge deletes the brand profile first
    expect_sqlstate(
        db,
        "23503",
        "DELETE FROM tenants WHERE id = %s",
        (tenant_a,),
        constraint="fk_brand_profiles_tenant_id_tenants",
    )
    db.execute("DELETE FROM brand_profiles WHERE tenant_id = %s", (tenant_a,))
    db.execute("DELETE FROM tenants WHERE id = %s", (tenant_a,))
    assert set(_counts(db, tenant_a).values()) == {0}
    assert _counts(db, tenant_b) == before_b
    # users are global: the purge removes orphans separately (DATABASE.md §9)
    assert db.execute("SELECT count(*) FROM users").fetchone() == users_before


UPDATES: dict[str, str] = {
    "tenants": "name = name || '!'",
    "users": "display_name = 'renamed'",
    "tenant_memberships": "last_used_at = now()",
    "invitations": "revoked_at = now()",
    "business_parties": "city = 'Pune'",
    "brand_profiles": "brand_description = 'about'",
    "products": "description = 'about'",
    "product_colourways": "position = position + 1",
    "variant_matrix_cells": "evidence_ref = 'image:x'",
}


def _tables_with_updated_at(conn: Conn) -> list[str]:
    rows = conn.execute(
        "SELECT c.relname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' "
        "AND a.attname = 'updated_at' AND NOT a.attisdropped ORDER BY 1"
    ).fetchall()
    return [str(r[0]) for r in rows]


def test_updated_at_triggers(db: Conn) -> None:
    tables = _tables_with_updated_at(db)
    assert set(TABLES_0001) <= set(tables)
    for table in tables:  # catalog-wide: S7 tables inherit the check
        trigger = db.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal "
            "AND tgfoid = 'public.set_updated_at()'::regprocedure AND tgenabled = 'O' "
            "AND (tgtype & 2) = 2 AND (tgtype & 16) = 16 AND (tgtype & 1) = 1",
            (table,),
        ).fetchone()
        assert trigger == (1,), f"{table}: no BEFORE UPDATE FOR EACH ROW set_updated_at trigger"
    tenant_id = f.tenant(db)
    _graph(db, tenant_id)
    for table, assignment in UPDATES.items():
        identity = sql.SQL("tenant_id" if table == "brand_profiles" else "id")
        # backdate with triggers off (replica role), then a real UPDATE of another column
        db.execute("SET LOCAL session_replication_role = replica")
        db.execute(sql.SQL("UPDATE {} SET updated_at = '2000-01-01'").format(sql.Identifier(table)))
        db.execute("SET LOCAL session_replication_role = origin")
        backdated = db.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE updated_at = '2000-01-01'").format(
                sql.Identifier(table)
            )
        ).fetchone()
        assert backdated is not None and backdated[0] > 0, table
        db.execute(sql.SQL("UPDATE {} SET {}").format(sql.Identifier(table), sql.SQL(assignment)))
        stale = db.execute(
            sql.SQL("SELECT {} FROM {} WHERE updated_at <> now()").format(
                identity, sql.Identifier(table)
            )
        ).fetchall()
        assert stale == [], table


def test_head_has_no_drift(
    head_db: ScratchDb, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.core.config import get_settings
    from app.db.migrations import alembic_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", head_db.url)
    get_settings.cache_clear()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # a computed-column mismatch is only a warning
            command.check(alembic_config())
    finally:
        get_settings.cache_clear()


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


def test_0001_tenant_fks_composite(db: Conn) -> None:
    """The 0001 tables' FKs; test_0002_catalog.py checks the whole catalog from 0002 on."""
    fks = [fk for fk in _tenant_fks(db) if fk[1] in TABLES_0001]
    assert len(fks) >= 18
    for name, source, target, columns, target_columns in fks:
        if name == SAME_STYLE_FK:
            assert source == "variant_matrix_cells" and "tenant_id" not in columns
            pinned = [
                fk for fk in fks if fk[1] == source and fk[3] == ["tenant_id", "style_product_id"]
            ]
            assert pinned, "the same-style FK needs the composite (tenant_id, style_product_id) FK"
            continue
        assert "tenant_id" in columns, f"{name}: {source}{columns} → {target} without tenant_id"
        paired = target_columns[columns.index("tenant_id")]
        assert paired == "tenant_id", f"{name}: tenant_id pairs with {target}.{paired}"


def test_0001_tenant_fks_cover_every_column(db: Conn) -> None:
    """A new FK column between tenant tables without a cross-tenant 23503 test goes red."""
    tested = {
        *test_0001_memberships.CROSS_TENANT_FK_COLUMNS,
        *test_0001_parties.CROSS_TENANT_FK_COLUMNS,
        *test_0001_products_family.CROSS_TENANT_FK_COLUMNS,
        *test_0001_products_facts.CROSS_TENANT_FK_COLUMNS,
        *test_0001_variants.CROSS_TENANT_FK_COLUMNS,
    }
    in_catalog = {
        f"{source}.{column}"
        for _, source, _, columns, _ in _tenant_fks(db)
        if source in TABLES_0001
        for column in columns
        if column != "tenant_id"
    }
    assert len(tested) == 19
    assert tested == in_catalog


def _public_0001_objects(db: ScratchDb) -> dict[str, list[tuple[object, ...]]]:
    with psycopg.connect(db.libpq, autocommit=True, connect_timeout=5) as conn:
        snapshot = catalog_snapshot(conn)
        triggers = conn.execute(
            "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal ORDER BY 1, 2"
        ).fetchall()
    return {**snapshot, "triggers": triggers}


def test_0001_round_trip(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    empty = _public_0001_objects(scratch_db)
    command.upgrade(cfg, "head")
    at_head = _public_0001_objects(scratch_db)
    relations = {str(r[0]) for r in at_head["relations"]}
    assert set(TABLES_0001) <= relations
    assert {str(r[0]) for r in at_head["functions"]} >= set(FUNCTIONS_0001)
    # one set_updated_at trigger per 0001 table (later revisions add their own tables' triggers)
    triggers_0001 = [r for r in at_head["triggers"] if r[0] in TABLES_0001]
    assert sorted(triggers_0001) == sorted((t, "set_updated_at") for t in TABLES_0001)
    command.downgrade(cfg, "base")
    after = _public_0001_objects(scratch_db)
    assert after["functions"] == empty["functions"] == []
    assert after["triggers"] == []
    alembic_own = {"alembic_version", "alembic_version_pkc"}
    assert [r for r in after["relations"] if r[0] not in alembic_own] == []
    assert after["versions"] == []
    command.upgrade(cfg, "head")
    assert _public_0001_objects(scratch_db) == at_head


def test_0001_failed_upgrade_leaves_nothing(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    """0001 is one transaction: a failure in its last slice leaves no table or function."""
    with psycopg.connect(scratch_db.libpq, autocommit=True, connect_timeout=5) as conn:
        conn.execute("CREATE TABLE variant_matrix_cells (id int)")
    with pytest.raises(Exception, match="already exists"):
        command.upgrade(alembic_env(None), "head")
    with psycopg.connect(scratch_db.libpq, autocommit=True, connect_timeout=5) as conn:
        assert conn.execute("SELECT to_regclass('public.tenants')").fetchone() == (None,)
        assert conn.execute("SELECT to_regprocedure('public.gtin_valid(text)')").fetchone() == (
            None,
        )
        if conn.execute("SELECT to_regclass('alembic_version')").fetchone() != (None,):
            assert conn.execute("SELECT count(*) FROM alembic_version").fetchone() == (0,)
