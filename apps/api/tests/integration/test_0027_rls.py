"""0027 `import_column_maps` (SI14 T1, AC2): tenant RLS with FORCE, no DELETE for app_rw, and the
seller `samples` cells kept off app_ro and the whole table off app_platform_ro."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


def _map(db: Conn, tenant: UUID) -> UUID:
    return f.insert(  # type: ignore[no-any-return]
        db,
        "import_column_maps",
        tenant_id=tenant,
        status="ready",
        headers=["item sku"],
        headers_digest="0" * 64,
        mappings=Jsonb([]),
        samples={"item sku": ["A-1"]},
        created_by_user_id=f.member(db, tenant),
    )


def test_force_rls_is_on(db: Conn) -> None:
    assert scalar(
        db,
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
        "WHERE oid = 'public.import_column_maps'::regclass",
    )


def test_app_rw_sees_only_its_tenant(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    _map(db, a)
    other = _map(db, b)
    user_b = scalar(db, "SELECT created_by_user_id FROM import_column_maps WHERE id = %s", (other,))
    with as_app_rw(db, a):
        ids = db.execute("SELECT tenant_id FROM import_column_maps").fetchall()
        assert {row[0] for row in ids} == {a}
        expect_sqlstate(
            db,
            "42501",
            "INSERT INTO import_column_maps (tenant_id, status, headers, headers_digest, mappings, "
            "created_by_user_id) VALUES (%s, 'ready', ARRAY['sku'], repeat('0', 64), '[]', %s)",
            (b, user_b),
        )
        expect_sqlstate(db, "42501", "DELETE FROM import_column_maps")


def test_app_ro_never_reads_samples(db: Conn) -> None:
    _map(db, f.tenant(db))
    db.execute("SET ROLE app_ro")
    try:
        assert scalar(db, "SELECT count(*) FROM import_column_maps") >= 1
        expect_sqlstate(db, "42501", "SELECT samples FROM import_column_maps")
    finally:
        db.execute("RESET ROLE")


def test_app_ro_reads_the_new_columns(db: Conn) -> None:
    db.execute("SET ROLE app_ro")
    try:
        db.execute("SELECT category_suggestion, column_map_id FROM products, imports LIMIT 0")
    finally:
        db.execute("RESET ROLE")


def test_app_platform_ro_never_reads_column_maps(db: Conn) -> None:
    db.execute("SET ROLE app_platform_ro")
    try:
        expect_sqlstate(db, "42501", "SELECT id FROM import_column_maps")
    finally:
        db.execute("RESET ROLE")
