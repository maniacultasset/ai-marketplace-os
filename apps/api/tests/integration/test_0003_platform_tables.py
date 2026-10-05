"""The five platform-global tables of revision 0003_ops_tables (DATABASE.md §5.2
category_templates and marketplace_error_map, §5.6 attribute_definitions, attribute_columns and
category_attributes): no tenant_id, the template version keys, the dictionary key regex and the
criticality CHECKs, the RESTRICT FKs and the error-map fix pair. Rows are written as `migrator`,
the tables' real writer."""

from __future__ import annotations

import re
from typing import Any

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from . import factories as f
from .conftest import expect_sqlstate
from .ops_support import (
    attribute_column,
    attribute_definition,
    category_attribute,
    category_template,
    digest,
    error_map,
)

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

PLATFORM_TABLES = (
    "category_templates",
    "marketplace_error_map",
    "attribute_definitions",
    "attribute_columns",
    "category_attributes",
)
KEY_RE = re.compile(r"[a-z][a-z0-9_]{1,63}")


@pytest.fixture
def mig(db: Conn) -> Conn:
    db.execute("SET LOCAL ROLE migrator")
    return db


def test_platform_tables_no_tenant_id(db: Conn) -> None:
    rows = db.execute(
        "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND column_name = 'tenant_id' AND table_name = ANY(%s)",
        (list(PLATFORM_TABLES),),
    ).fetchall()
    assert rows == []
    present = db.execute(
        "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
        "AND relkind = 'r' AND relname = ANY(%s)",
        (list(PLATFORM_TABLES),),
    ).fetchone()
    assert present == (5,)


def test_platform_tables_ct_one_current(mig: Conn) -> None:
    category_template(mig, category="tshirts", version=1, is_current=True)
    expect_sqlstate(
        mig,
        "23505",
        "INSERT INTO category_templates (marketplace, category, version, is_current, "
        "product_type, source, attributes) VALUES ('amazon', 'tshirts', 2, true, 'SHIRT', "
        '\'manual\', \'{"required_attributes": [], "optional_attributes": [], "specs": {}}\')',
        constraint="ct_one_current",
    )
    category_template(mig, category="tshirts", version=2, is_current=False)
    # the same category on the other marketplace has its own current row
    category_template(mig, marketplace="flipkart", category="tshirts", version=1, is_current=True)


def test_platform_tables_ct_version_key(mig: Conn) -> None:
    category_template(mig, category="kurtas", version=1)
    try:
        with mig.transaction():
            category_template(mig, category="kurtas", version=1, is_current=False)
    except psycopg.Error as error:
        assert error.sqlstate == "23505"
        assert error.diag.constraint_name == "ct_version_key"
    else:
        raise AssertionError("a duplicate (marketplace, category, version) inserted")


def test_platform_tables_ct_fetched(mig: Conn) -> None:
    for missing in ({"fetched_at": None}, {"source_digest": None}):
        columns: dict[str, Any] = {
            "source": "ptd",
            "fetched_at": "2026-10-05T00:00:00Z",
            "source_digest": digest("ptd"),
            **missing,
        }
        try:
            with mig.transaction():
                category_template(mig, **columns)
        except psycopg.Error as error:
            assert error.sqlstate == "23514"
            assert error.diag.constraint_name == "ct_fetched"
        else:
            raise AssertionError(f"a ptd template without {missing} inserted")
    category_template(mig, source="manual")
    category_template(
        mig, source="ptd", fetched_at="2026-10-05T00:00:00Z", source_digest=digest("ok")
    )


def test_platform_tables_ct_marketplace_check(mig: Conn) -> None:
    try:
        with mig.transaction():
            category_template(mig, marketplace="ebay")
    except psycopg.Error as error:
        assert error.sqlstate == "23514"
        assert error.diag.constraint_name == "ck_category_templates_marketplace"
    else:
        raise AssertionError("an ebay template inserted")


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"source": "scraped"}, "ck_category_templates_source"),
        ({"version": 0}, "ck_category_templates_version"),
        ({"attributes": {"specs": {}}}, "ck_category_templates_attributes"),
        ({"raw_schema_key": "https://x.test/t.json"}, "ck_category_templates_raw_schema_key"),
        (
            {"source": "ptd", "fetched_at": "2026-10-05T00:00:00Z", "source_digest": b"\x01" * 31},
            "ck_category_templates_source_digest",
        ),
    ],
)
def test_platform_tables_ct_checks(mig: Conn, columns: dict[str, Any], constraint: str) -> None:
    try:
        with mig.transaction():
            category_template(mig, **columns)
    except psycopg.Error as error:
        assert error.sqlstate == "23514"
        assert error.diag.constraint_name == constraint
    else:
        raise AssertionError(f"{columns} inserted")


@pytest.mark.parametrize("key", ["Fabric", "1abc", "a", "a" * 65, "fabrïc", "", " ", "fab ric"])
def test_platform_tables_ad_key_regex(mig: Conn, key: str) -> None:
    try:
        with mig.transaction():
            attribute_definition(mig, key=key)
    except psycopg.Error as error:
        assert error.sqlstate == "23514"
        assert error.diag.constraint_name == "ck_attribute_definitions_key"
    else:
        raise AssertionError(f"key {key!r} inserted")


def test_platform_tables_ad_key_regex_accepts(mig: Conn) -> None:
    for key in ("fabric_type", "ab", "a" * 64, "gsm2"):
        attribute_definition(mig, key=key)


_PG_TEXT = st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), max_size=70)


@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    candidate=st.one_of(
        _PG_TEXT,
        st.from_regex(r"[a-z][a-z0-9_]{1,63}", fullmatch=True),
        st.from_regex(r"[a-z][a-z0-9_]{1,63}\n", fullmatch=True),
    )
)
def test_platform_tables_ad_key_regex_matches_python(db: Conn, candidate: str) -> None:
    pattern = db.execute(
        "SELECT substring(pg_get_constraintdef(oid) FROM '''(.*)''') FROM pg_constraint "
        "WHERE conname = 'ck_attribute_definitions_key'"
    ).fetchone()
    assert pattern is not None
    row = db.execute("SELECT %s ~ %s", (candidate, pattern[0])).fetchone()
    assert row is not None
    assert row[0] == (KEY_RE.fullmatch(candidate) is not None), candidate


def test_platform_tables_ad_critical_never_ai(mig: Conn) -> None:
    try:
        with mig.transaction():
            attribute_definition(
                mig, is_critical=True, allowed_sources=["seller", "ai"], confirm_agent_captures=True
            )
    except psycopg.Error as error:
        assert error.sqlstate == "23514"
        assert error.diag.constraint_name == "ck_ad_critical_never_ai"
    else:
        raise AssertionError("a critical key allowing ai inserted")
    attribute_definition(mig, is_critical=False, allowed_sources=["seller", "ai"])


def test_platform_tables_ad_critical_confirms(mig: Conn) -> None:
    try:
        with mig.transaction():
            attribute_definition(
                mig, is_critical=True, allowed_sources=["seller"], confirm_agent_captures=False
            )
    except psycopg.Error as error:
        assert error.sqlstate == "23514"
        assert error.diag.constraint_name == "ck_ad_critical_confirms"
    else:
        raise AssertionError("a critical key without confirmation inserted")
    attribute_definition(
        mig, is_critical=True, allowed_sources=["seller"], confirm_agent_captures=True
    )


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"section": "misc"}, "ck_attribute_definitions_section"),
        ({"storage": "json"}, "ck_attribute_definitions_storage"),
        ({"value_type": "date"}, "ck_attribute_definitions_value_type"),
        ({"owner_levels": []}, "ck_attribute_definitions_owner_levels"),
        ({"owner_levels": ["style", "family"]}, "ck_attribute_definitions_owner_levels"),
        ({"min_confidence": 1.5}, "ck_attribute_definitions_min_confidence"),
    ],
)
def test_platform_tables_ad_vocab(mig: Conn, columns: dict[str, Any], constraint: str) -> None:
    try:
        with mig.transaction():
            attribute_definition(mig, **columns)
    except psycopg.Error as error:
        assert error.sqlstate == "23514"
        assert error.diag.constraint_name == constraint
    else:
        raise AssertionError(f"{columns} inserted")


def test_platform_tables_ad_revision_from_rules_seq(mig: Conn) -> None:
    first = attribute_definition(mig)
    second = attribute_definition(mig)
    rows = mig.execute(
        "SELECT key, revision FROM attribute_definitions WHERE key = ANY(%s) ORDER BY revision",
        ([first, second],),
    ).fetchall()
    assert [r[0] for r in rows] == [first, second]
    assert rows[0][1] < rows[1][1]
    # both drawn from rules_revision_seq: a rule inserted next gets a larger revision
    rule_revision = mig.execute(
        "SELECT revision FROM rules WHERE id = %s", (f.rule(mig),)
    ).fetchone()
    assert rule_revision is not None and rule_revision[0] > rows[1][1]
    default = mig.execute(
        "SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef d JOIN pg_attribute a "
        "ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
        "WHERE d.adrelid = ANY(ARRAY['attribute_definitions'::regclass, "
        "'category_attributes'::regclass]) AND a.attname = 'revision'"
    ).fetchall()
    assert default == [("nextval('rules_revision_seq'::regclass)",)] * 2


def test_platform_tables_ca_override_false_refused(mig: Conn) -> None:
    key = attribute_definition(mig)
    try:
        with mig.transaction():
            category_attribute(mig, key, critical_override=False)
    except psycopg.Error as error:
        assert error.sqlstate == "23514"
        assert error.diag.constraint_name == "ck_category_attributes_critical_override"
    else:
        raise AssertionError("critical_override = false inserted")
    category_attribute(mig, key, category="tshirts", critical_override=None)
    category_attribute(mig, key, category="kurtas", critical_override=True)


def test_platform_tables_ca_relevance_check(mig: Conn) -> None:
    key = attribute_definition(mig)
    try:
        with mig.transaction():
            category_attribute(mig, key, relevance="rare")
    except psycopg.Error as error:
        assert error.sqlstate == "23514"
        assert error.diag.constraint_name == "ck_category_attributes_relevance"
    else:
        raise AssertionError("relevance 'rare' inserted")


def test_platform_tables_attr_fk_restrict(mig: Conn) -> None:
    key = attribute_definition(mig, storage="column")
    attribute_column(mig, key)
    expect_sqlstate(
        mig,
        "23503",
        "DELETE FROM attribute_definitions WHERE key = %s",
        (key,),
        constraint="fk_attribute_columns_attribute_key_attribute_definitions",
    )
    other = attribute_definition(mig)
    category_attribute(mig, other)
    expect_sqlstate(
        mig,
        "23503",
        "DELETE FROM attribute_definitions WHERE key = %s",
        (other,),
        constraint="fk_category_attributes_attribute_key_attribute_definitions",
    )
    expect_sqlstate(
        mig,
        "23503",
        "INSERT INTO attribute_columns (table_name, column_name, attribute_key) "
        "VALUES ('products', 'gsm', 'no_such_key')",
    )


def test_platform_tables_attr_fk_is_restrict(db: Conn) -> None:
    rows = db.execute(
        "SELECT conname, confdeltype FROM pg_constraint WHERE contype = 'f' "
        "AND confrelid = 'attribute_definitions'::regclass ORDER BY 1"
    ).fetchall()
    assert rows == [
        ("fk_attribute_columns_attribute_key_attribute_definitions", "r"),
        ("fk_category_attributes_attribute_key_attribute_definitions", "r"),
    ]


def test_platform_tables_ac_table_name_check(mig: Conn) -> None:
    key = attribute_definition(mig, storage="column")
    try:
        with mig.transaction():
            attribute_column(mig, key, table_name="listings")
    except psycopg.Error as error:
        assert error.sqlstate == "23514"
        assert error.diag.constraint_name == "ck_attribute_columns_table_name"
    else:
        raise AssertionError("table_name 'listings' inserted")
    attribute_column(mig, key, table_name="brand_profiles", column_name="default_gsm")


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"auto_fixable": True, "fix_action": None}, "mem_fix"),
        ({"auto_fixable": False, "fix_action": "reformat"}, "mem_fix"),
        ({"auto_fixable": True, "fix_action": "guess"}, "mem_fix_action"),
        ({"error_class": "unknown"}, "ck_marketplace_error_map_error_class"),
        ({"match_on": "message"}, "ck_marketplace_error_map_match_on"),
        ({"retry_class": "never"}, "ck_marketplace_error_map_retry_class"),
        ({"severity": "info"}, "ck_marketplace_error_map_severity"),
        ({"marketplace": "ebay"}, "ck_marketplace_error_map_marketplace"),
        ({"version": 0}, "ck_marketplace_error_map_version"),
        ({"plain_text": "x" * 501}, "ck_marketplace_error_map_plain_text"),
    ],
)
def test_platform_tables_mem_checks(mig: Conn, columns: dict[str, Any], constraint: str) -> None:
    try:
        with mig.transaction():
            error_map(mig, **columns)
    except psycopg.Error as error:
        assert error.sqlstate == "23514"
        assert error.diag.constraint_name == constraint
    else:
        raise AssertionError(f"{columns} inserted")


def test_platform_tables_mem_fix(mig: Conn) -> None:
    error_map(mig, auto_fixable=True, fix_action="reformat")
    error_map(mig, auto_fixable=False, fix_action=None)
    error_map(mig, plain_text="x" * 500)


def test_platform_tables_mem_key(mig: Conn) -> None:
    error_map(mig, raw_value="8541", version=1)
    error_map(mig, raw_value="8541", version=2)
    error_map(mig, raw_value="8541", version=1, match_on="category")
    try:
        with mig.transaction():
            error_map(mig, raw_value="8541", version=1)
    except psycopg.Error as error:
        assert error.sqlstate == "23505"
        assert error.diag.constraint_name == "mem_key"
    else:
        raise AssertionError("a duplicate (marketplace, match_on, raw_value, version) inserted")


def test_platform_tables_mem_lookup_index(db: Conn) -> None:
    row = db.execute(
        "SELECT pg_get_indexdef('ix_marketplace_error_map_lookup'::regclass)"
    ).fetchone()
    assert row is not None
    assert row[0].endswith("(marketplace, match_on, raw_value, version DESC) WHERE enabled"), row[0]
