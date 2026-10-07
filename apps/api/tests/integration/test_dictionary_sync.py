"""S13 B5 (Q6; DATABASE.md:1152-1179, :507): `dictionary sync`, the migrator's upsert of
`attribute_dictionary.json`. A re-sync of an unchanged file writes no row and moves no
`revision`, so `rules_version` stays put; an edited key moves only its own row. Each test ends with
the shipped file synced, so the shared test database keeps the canonical dictionary."""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from app.db.platform_loads import load_dictionary, sync_dictionary
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .conftest import LOCAL_HOSTS, Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
async def migrator_engine(superuser_conn: Conn) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    await sync_dictionary(engine)
    yield engine
    await sync_dictionary(engine)
    await engine.dispose()


def revisions(conn: Conn) -> dict[str, int]:
    rows = conn.execute("SELECT key, revision FROM attribute_definitions").fetchall()
    return {str(k): int(str(r)) for k, r in rows}


def rules_version(conn: Conn) -> int:
    """DATABASE.md:507: max(revision) over the rules rows plus every dictionary row."""
    row = conn.execute(
        "SELECT greatest((SELECT max(revision) FROM rules), "
        "(SELECT max(revision) FROM attribute_definitions), "
        "(SELECT coalesce(max(revision), 0) FROM category_attributes))"
    ).fetchone()
    assert row is not None
    return int(str(row[0]))


async def test_dictionary_sync_idempotent(
    migrator_engine: AsyncEngine, superuser_conn: Conn
) -> None:
    before = revisions(superuser_conn)
    counts = await sync_dictionary(migrator_engine)
    assert (counts.inserted, counts.updated, counts.disabled) == (0, 0, 0)
    assert revisions(superuser_conn) == before
    max_ca = superuser_conn.execute("SELECT max(revision) FROM category_attributes").fetchone()
    counts = await sync_dictionary(migrator_engine)
    assert counts.updated == 0
    assert (
        superuser_conn.execute("SELECT max(revision) FROM category_attributes").fetchone() == max_ca
    )


async def test_dictionary_sync_changed_key_bumps_once(
    migrator_engine: AsyncEngine, superuser_conn: Conn, tmp_path: Path
) -> None:
    doc = json.loads(load_dictionary().model_dump_json())
    for entry in doc["attributes"]:
        if entry["key"] == "season":
            entry["brd_ref"] = "edited by the test"
    edited = tmp_path / "dictionary.json"
    edited.write_text(json.dumps(doc), encoding="utf-8")
    before = revisions(superuser_conn)
    counts = await sync_dictionary(migrator_engine, edited)
    assert (counts.inserted, counts.updated) == (0, 1)
    after = revisions(superuser_conn)
    moved = {k for k in after if after[k] != before[k]}
    assert moved == {"season"}
    counts = await sync_dictionary(migrator_engine, edited)
    assert counts.updated == 0
    assert revisions(superuser_conn) == after


async def test_dictionary_resync_keeps_rules_version(
    migrator_engine: AsyncEngine, superuser_conn: Conn
) -> None:
    before = rules_version(superuser_conn)
    await sync_dictionary(migrator_engine)
    await sync_dictionary(migrator_engine)
    assert rules_version(superuser_conn) == before


async def test_dictionary_sync_after_demo_seed(
    migrator_engine: AsyncEngine, superuser_conn: Conn
) -> None:
    from app.db.seed_ops import seed_attribute_definitions

    async with migrator_engine.begin() as connection:
        await seed_attribute_definitions(connection)
    await sync_dictionary(migrator_engine)
    rows = superuser_conn.execute(
        "SELECT key, enabled, input_aliases FROM attribute_definitions "
        "WHERE key IN ('color', 'fabric_type') ORDER BY key"
    ).fetchall()
    assert rows == [("color", True, []), ("fabric_type", True, ["material"])]


def _typed_columns(conn: Conn, table: str) -> set[str]:
    """Every fact column of `table`: the ones `ck_products_typed_facts_sourced` names, the
    party ids, and (brand_profiles) every column but the keys, timestamps and provenance."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        [table],
    ).fetchall()
    names = {str(r[0]) for r in rows}
    not_facts = {
        "id", "tenant_id", "sku", "status", "description", "source_image_urls",
        "parent_product_id", "variation_theme", "size_run", "variant_attributes", "variant_key",
        "currency", "attributes", "provenance", "created_by", "deleted_at", "created_at",
        "updated_at", "onboarding_completed_at",
    }  # fmt: skip
    return names - not_facts


def test_dictionary_sync_typed_columns_each_one_key(superuser_conn: Conn) -> None:
    mapped = superuser_conn.execute(
        "SELECT table_name, column_name, attribute_key FROM attribute_columns"
    ).fetchall()
    by_column: dict[tuple[str, str], list[str]] = {}
    for table, column, key in mapped:
        by_column.setdefault((str(table), str(column)), []).append(str(key))
    for table in ("products", "brand_profiles"):
        for column in _typed_columns(superuser_conn, table):
            assert len(by_column.get((table, column), [])) == 1, (table, column)
    # the pairs agree with ck_products_typed_facts_sourced
    definition = superuser_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_products_typed_facts_sourced'"
    ).fetchone()
    assert definition is not None
    pairs = re.findall(r"\((\w+) IS NULL\) OR \(provenance \? '(\w+)'::text\)", str(definition[0]))
    assert pairs, "the constraint was not parsed"
    for column, key in pairs:
        assert by_column[("products", column)] == [key], column
    disabled_columns = superuser_conn.execute(
        "SELECT count(*) FROM attribute_columns c JOIN attribute_definitions d "
        "ON d.key = c.attribute_key WHERE d.storage <> 'column'"
    ).fetchone()
    assert disabled_columns == (0,)


def test_dictionary_sync_app_rw_cannot_write(superuser_conn: Conn) -> None:
    from tests.support.dsn import as_app_rw

    from .conftest import libpq_url

    url = libpq_url(as_app_rw(os.environ["DATABASE_URL"]))
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        for statement in (
            "UPDATE attribute_definitions SET enabled = false WHERE key = 'color'",
            "INSERT INTO attribute_columns (table_name, column_name, attribute_key) "
            "VALUES ('products', 'sku', 'title')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_dictionary_file_matches_loader_rules() -> None:
    dictionary = load_dictionary()
    keys: dict[str, Any] = {e.key: e for e in dictionary.attributes}
    assert keys["fabric_type"].input_aliases == ["material"]
    assert keys["mrp"].is_critical and "ai" not in keys["mrp"].allowed_sources


def test_dictionary_sync_cli_one_line(
    superuser_conn: Conn,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import main
    from app.core.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", os.environ["DATABASE_URL"])
    get_settings.cache_clear()
    try:
        assert main(["dictionary", "sync"]) == 0
        out = capsys.readouterr()
        assert out.out == "dictionary sync: ok (0 inserted, 0 updated, 0 disabled)\n"
        broken = tmp_path / "broken.json"
        broken.write_text('{"dictionary_version": 0}', encoding="utf-8")
        assert main(["dictionary", "sync", "--file", str(broken)]) == 1
        assert capsys.readouterr().err == "dictionary sync: failed (ValidationError)\n"
    finally:
        get_settings.cache_clear()
