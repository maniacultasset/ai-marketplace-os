"""Revision 0014_product_type_definitions (DW2): the round trip, the grants, the content guard,
the one-current index, the CHECKs, the audit allow-list entry and the column list (no personal
column). Run in the Docker batch (`make test-db`, `make api-tests`)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import as_migrator, scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0013_trusted_devices"
REVISION = "0014_product_type_definitions"
COLUMNS = (
    "id",
    "marketplace",
    "marketplace_id",
    "product_type",
    "requirements",
    "locale",
    "product_type_version",
    "requirements_enforced",
    "display_name",
    "property_groups",
    "schema_json",
    "meta_schema_id",
    "schema_digest",
    "fetched_at",
    "is_current",
    "created_at",
)
AUDIT_SQL = "SELECT audit(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
INSERT = (
    "INSERT INTO product_type_definitions (marketplace, marketplace_id, product_type, "
    "requirements, locale, product_type_version, requirements_enforced, display_name, "
    "property_groups, schema_json, meta_schema_id, schema_digest, fetched_at, is_current) "
    "VALUES (%(marketplace)s, %(marketplace_id)s, %(product_type)s, %(requirements)s, "
    "%(locale)s, %(product_type_version)s, %(requirements_enforced)s, %(display_name)s, "
    "%(property_groups)s::jsonb, %(schema_json)s::jsonb, %(meta_schema_id)s, %(schema_digest)s, "
    "now(), %(is_current)s) RETURNING id"
)


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "marketplace": "amazon",
        "marketplace_id": "A21TJRUUN4KGV",
        "product_type": "KURTA",
        "requirements": "LISTING_PRODUCT_ONLY",
        "locale": "en_IN",
        "product_type_version": "U1234",
        "requirements_enforced": "ENFORCED",
        "display_name": "Kurta",
        "property_groups": json.dumps({"offer": {"propertyNames": ["item_name"]}}),
        "schema_json": json.dumps({"type": "object"}),
        "meta_schema_id": "https://schemas.amazon.com/selling-partners/definitions/"
        "product-types/meta-schema/v1",
        "schema_digest": b"\x01" * 32,
        "is_current": False,
    }
    row.update(overrides)
    return row


def _insert(conn: Conn, **overrides: Any) -> UUID:
    found = conn.execute(INSERT, _row(**overrides)).fetchone()
    assert found is not None
    return UUID(str(found[0]))


def _table_exists(db: ScratchDb) -> bool:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        return bool(
            scalar(conn, "SELECT to_regclass('public.product_type_definitions') IS NOT NULL")
        )


def _allowlist_def(db: ScratchDb) -> str:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        return str(
            scalar(
                conn, "SELECT md5(pg_get_functiondef('public.audit_allowlist(text)'::regprocedure))"
            )
        )


# ---- 1. round trip ----


def test_round_trip_up_down_up(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    before = _allowlist_def(scratch_db)
    assert not _table_exists(scratch_db)
    command.upgrade(cfg, REVISION)
    at_head = _allowlist_def(scratch_db)
    assert _table_exists(scratch_db) and at_head != before
    command.upgrade(cfg, "head")
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert not _table_exists(scratch_db)
    assert _allowlist_def(scratch_db) == before
    command.upgrade(cfg, REVISION)
    assert _table_exists(scratch_db) and _allowlist_def(scratch_db) == at_head
    command.upgrade(cfg, "head")
    command.check(cfg)


# ---- 2. grants ----


@pytest.mark.parametrize("role", ["app_rw", "app_ro", "app_platform_ro"])
def test_app_roles_read_but_never_write(db: Conn, role: str) -> None:
    with as_migrator(db):
        row_id = _insert(db)
    db.execute(f"SET ROLE {role}")
    try:
        assert scalar(db, "SELECT count(*) FROM product_type_definitions") == 1
        expect_sqlstate(db, "42501", INSERT, _row(schema_digest=b"\x02" * 32))
        expect_sqlstate(
            db,
            "42501",
            "UPDATE product_type_definitions SET is_current = true WHERE id = %s",
            (row_id,),
        )
        expect_sqlstate(
            db, "42501", "DELETE FROM product_type_definitions WHERE id = %s", (row_id,)
        )
        expect_sqlstate(db, "42501", "TRUNCATE product_type_definitions")
    finally:
        db.execute("RESET ROLE")


# ---- 3. guard ----


def test_migrator_inserts_and_moves_is_current_only(db: Conn) -> None:
    with as_migrator(db):
        row_id = _insert(db)
        db.execute("UPDATE product_type_definitions SET is_current = true WHERE id = %s", (row_id,))
        assert scalar(
            db, "SELECT is_current FROM product_type_definitions WHERE id = %s", (row_id,)
        )
        for column, value in (
            ("schema_json", '\'{"type": "string"}\'::jsonb'),
            ("product_type", "'SHIRT'"),
            ("schema_digest", "'\\x" + "02" * 32 + "'::bytea"),
        ):
            expect_sqlstate(
                db,
                "GRD01",
                f"UPDATE product_type_definitions SET {column} = {value} WHERE id = %s",  # noqa: S608
                (row_id,),
            )
        expect_sqlstate(
            db, "GRD01", "DELETE FROM product_type_definitions WHERE id = %s", (row_id,)
        )


# ---- 4. one current row per key ----


def test_one_current_row_per_key(db: Conn) -> None:
    with as_migrator(db):
        _insert(db, is_current=True)
        _insert(db, schema_digest=b"\x02" * 32, product_type_version="U2")
        _insert(db, schema_digest=b"\x03" * 32, product_type_version="U3")
        expect_sqlstate(
            db,
            "23505",
            INSERT,
            _row(schema_digest=b"\x04" * 32, is_current=True),
            constraint="ptd_one_current",
        )
        # another locale is another key
        _insert(db, locale="hi_IN", is_current=True)
        expect_sqlstate(db, "23505", INSERT, _row(), constraint="ptd_version_key")


# ---- 5-7. CHECKs ----


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"schema_digest": b"\x01" * 31}, "ck_product_type_definitions_schema_digest"),
        ({"schema_digest": b"\x01" * 33}, "ck_product_type_definitions_schema_digest"),
        ({"product_type": "kurta"}, "ck_product_type_definitions_product_type"),
        ({"product_type": "KURTA/../X"}, "ck_product_type_definitions_product_type"),
        ({"product_type": "A" * 65}, "ck_product_type_definitions_product_type"),
        ({"product_type": ""}, "ck_product_type_definitions_product_type"),
        ({"requirements": "OFFER"}, "ck_product_type_definitions_requirements"),
        ({"locale": "en-IN"}, "ck_product_type_definitions_locale"),
        (
            {"meta_schema_id": "https://x.amazonaws.com/s?X-Amz-Signature=abc"},
            "ck_product_type_definitions_meta_schema_id",
        ),
        ({"schema_json": "[]"}, "ck_product_type_definitions_schema_json"),
        ({"marketplace": "flipkart"}, "ck_product_type_definitions_marketplace"),
        ({"marketplace_id": "a21"}, "ck_product_type_definitions_marketplace_id"),
        ({"requirements_enforced": "MAYBE"}, "ck_product_type_definitions_enforced"),
        ({"product_type_version": ""}, "ck_product_type_definitions_version"),
        ({"display_name": "x" * 257}, "ck_product_type_definitions_display_name"),
    ],
)
def test_checks(db: Conn, overrides: dict[str, Any], constraint: str) -> None:
    with as_migrator(db):
        expect_sqlstate(db, "23514", INSERT, _row(**overrides), constraint=constraint)


def test_unicode_display_name_round_trips(db: Conn) -> None:
    with as_migrator(db):
        row_id = _insert(db, display_name="कुर्ता")
    assert (
        scalar(db, "SELECT display_name FROM product_type_definitions WHERE id = %s", (row_id,))
        == "कुर्ता"
    )


# ---- 8. audit allow-list ----


def _audit(db: Conn, after: dict[str, Any], entity_id: UUID) -> None:
    db.execute(
        AUDIT_SQL,
        (
            None,
            "staff",
            None,
            "product_type_definition.stored",
            "product_type_definition",
            entity_id,
            None,
            json.dumps(after),
            "test",
            "req-1",
            None,
            None,
            "staff@example.com",
        ),
    )


def test_audit_keeps_only_allow_listed_keys(db: Conn) -> None:
    with as_migrator(db):
        row_id = _insert(db)
        _audit(
            db,
            {
                "marketplace_id": "A21TJRUUN4KGV",
                "product_type": "KURTA",
                "requirements": "LISTING_PRODUCT_ONLY",
                "locale": "en_IN",
                "product_type_version": "U1234",
                "link": "https://x.amazonaws.com/s?X-Amz-Signature=abc",
                "schema": {"type": "object"},
            },
            row_id,
        )
    after = scalar(
        db, "SELECT after FROM audit_log WHERE entity_id = %s AND tenant_id IS NULL", (row_id,)
    )
    assert set(after) == {
        "marketplace_id",
        "product_type",
        "requirements",
        "locale",
        "product_type_version",
    }


def test_audit_entity_unknown_after_downgrade(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq) as conn:
        expect_sqlstate(
            conn,
            "GRD01",
            "SELECT public.audit_allowlist('product_type_definition')",
        )
    command.upgrade(cfg, "head")


# ---- 9. column list: exactly the DDL, no personal column ----


def test_column_list_is_exact_and_holds_no_personal_column(db: Conn) -> None:
    rows = db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'product_type_definitions' ORDER BY ordinal_position"
    ).fetchall()
    names = tuple(r[0] for r in rows)
    assert names == COLUMNS
    import re

    personal = re.compile(r"email|_by$|actor|ip|name$")
    assert [n for n in names if personal.search(n) and n != "display_name"] == []
