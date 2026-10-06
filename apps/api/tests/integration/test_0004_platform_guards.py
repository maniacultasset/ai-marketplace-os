"""0004/14_platform_guards (DATABASE.md §5.2 :504, :508 rules; :543 category_templates; :555
marketplace_error_map; §5.6 :1153 attribute_definitions, :1176 category_attributes)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import sql

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, as_migrator, scalar, set_gucs
from .ops_support import attribute_definition, category_attribute, category_template, error_map

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


def _revision(db: Conn, table: str, where: str, key: Any) -> int:
    query = sql.SQL("SELECT revision FROM {} WHERE {} = %s").format(
        sql.Identifier(table), sql.Identifier(where)
    )
    return int(scalar(db, query.as_string(db), (key,)))


def test_rules_only_enabled_changes(db: Conn) -> None:
    tenant = f.tenant(db)
    rule = f.rule(db, tenant)
    for assignment in ("severity = 'warning'", "version = 9", "category = 'kurtas'"):
        expect_sqlstate(
            db, "GRD01", f"UPDATE rules SET {assignment} WHERE id = %s", (rule,)  # noqa: S608
        )
    with as_app_rw(db):
        db.execute("UPDATE rules SET enabled = false WHERE id = %s", (rule,))
        expect_sqlstate(db, "42501", "UPDATE rules SET severity = 'warning' WHERE id = %s", (rule,))


def test_rules_revision_bumps(db: Conn) -> None:
    rule = f.rule(db, f.tenant(db))
    before = _revision(db, "rules", "id", rule)
    db.execute("UPDATE rules SET enabled = false WHERE id = %s", (rule,))
    middle = _revision(db, "rules", "id", rule)
    db.execute("UPDATE rules SET enabled = false WHERE id = %s", (rule,))  # every UPDATE bumps
    db.execute("UPDATE rules SET revision = 1 WHERE id = %s", (rule,))  # never set by hand
    after = _revision(db, "rules", "id", rule)
    assert before < middle < after


def test_rules_delete_needs_purge_guc(db: Conn) -> None:
    tenant = f.tenant(db)
    rule, platform = f.rule(db, tenant), f.rule(db, None)
    expect_sqlstate(db, "GRD01", "DELETE FROM rules WHERE id = %s", (rule,))
    with as_app_rw(db):
        expect_sqlstate(db, "42501", "DELETE FROM rules WHERE id = %s", (rule,))
    with as_migrator(db):
        set_gucs(db, purge_tenant=tenant)
        expect_sqlstate(db, "GRD01", "DELETE FROM rules WHERE id = %s", (platform,))
        db.execute("DELETE FROM rules WHERE id = %s", (rule,))


def test_rules_app_rw_inserts_brand_rule(db: Conn) -> None:
    tenant = f.tenant(db)
    with as_app_rw(db):
        f.rule(db, tenant)
        expect_sqlstate(
            db,
            "42501",
            "INSERT INTO rules (tenant_id, marketplace, field, rule_type, subject, pack, rule_def, "
            "revision) VALUES (%s, 'amazon', 'content.title', 'banned_terms', '', 'brand', "
            '\'{"type": "banned_terms"}\', 1)',
            (tenant,),
        )


def test_template_content_immutable(db: Conn) -> None:
    template = category_template(db)
    for assignment in ("product_type = 'KURTA'", "version = 2", "attributes = '{}'"):
        expect_sqlstate(
            db,
            "GRD01",
            f"UPDATE category_templates SET {assignment} WHERE id = %s",  # noqa: S608
            (template,),
        )
    db.execute("UPDATE category_templates SET is_current = false WHERE id = %s", (template,))
    expect_sqlstate(db, "GRD01", "DELETE FROM category_templates WHERE id = %s", (template,))


def test_template_promote_two_updates(db: Conn) -> None:
    old = category_template(db, category="tshirts_x")
    new = category_template(db, category="tshirts_x", version=2, is_current=False)
    db.execute("UPDATE category_templates SET is_current = false WHERE id = %s", (old,))
    db.execute("UPDATE category_templates SET is_current = true WHERE id = %s", (new,))
    rows: dict[Any, Any] = dict(
        db.execute(
            "SELECT version, is_current FROM category_templates WHERE category = 'tshirts_x'"
        ).fetchall()
    )
    assert rows == {1: False, 2: True}


def test_error_map_only_enabled(db: Conn) -> None:
    row = error_map(db)
    db.execute("UPDATE marketplace_error_map SET enabled = false WHERE id = %s", (row,))
    expect_sqlstate(
        db, "GRD01", "UPDATE marketplace_error_map SET plain_text = 'x' WHERE id = %s", (row,)
    )
    expect_sqlstate(db, "GRD01", "DELETE FROM marketplace_error_map WHERE id = %s", (row,))


def test_dictionary_revision_bumps(db: Conn) -> None:
    key = attribute_definition(db)
    before = _revision(db, "attribute_definitions", "key", key)
    db.execute("UPDATE attribute_definitions SET enabled = false WHERE key = %s", (key,))
    assert _revision(db, "attribute_definitions", "key", key) > before
    expect_sqlstate(db, "GRD01", "DELETE FROM attribute_definitions WHERE key = %s", (key,))


def test_dictionary_noop_update_keeps_revision(db: Conn) -> None:
    key = attribute_definition(db)
    before = _revision(db, "attribute_definitions", "key", key)
    db.execute(
        "UPDATE attribute_definitions SET section = section, enabled = enabled WHERE key = %s",
        (key,),
    )
    db.execute("UPDATE attribute_definitions SET revision = 1 WHERE key = %s", (key,))
    assert _revision(db, "attribute_definitions", "key", key) == before


def test_category_attributes_noop_update_keeps_revision(db: Conn) -> None:
    category = category_attribute(db, attribute_definition(db))
    before = _revision(db, "category_attributes", "category", category)
    db.execute(
        "UPDATE category_attributes SET relevance = relevance WHERE category = %s", (category,)
    )
    assert _revision(db, "category_attributes", "category", category) == before
    db.execute("UPDATE category_attributes SET relevance = 'core' WHERE category = %s", (category,))
    assert _revision(db, "category_attributes", "category", category) > before
    expect_sqlstate(db, "GRD01", "DELETE FROM category_attributes WHERE category = %s", (category,))


@pytest.mark.parametrize(
    "table",
    [
        "category_templates",
        "marketplace_error_map",
        "attribute_definitions",
        "attribute_columns",
        "category_attributes",
        "listing_status_transitions",
    ],
)
def test_platform_tables_select_only(db: Conn, table: str) -> None:
    rows = db.execute(
        "SELECT privilege_type FROM information_schema.table_privileges "
        "WHERE table_name = %s AND grantee = 'app_rw' ORDER BY 1",
        (table,),
    ).fetchall()
    assert rows == [("SELECT",)]
    columns = db.execute(
        "SELECT DISTINCT privilege_type FROM information_schema.column_privileges "
        "WHERE table_name = %s AND grantee = 'app_rw' AND privilege_type <> 'SELECT'",
        (table,),
    ).fetchall()
    assert columns == []
