"""`rules` and `rules_revision_seq` (DATABASE.md §5.2; CANON-ADDENDUM.md A§4; KNOWN-ISSUES.md
KI-B rules key): the scope key with the promoted type, subject and pack, the brand-rule CHECKs,
the revision sequence and the tenant cascade."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_migrator, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


def test_rules_unique_includes_type(db: Conn) -> None:
    """Done-when 4: two rules that differ only in rule type, for one (marketplace, category,
    field, version), both insert."""
    f.rule(db, category="t_shirts")
    f.rule(
        db,
        category="t_shirts",
        rule_def=f.rule_def("min_length", limit=20, policy_ref=f.POLICY_REF),
    )
    rows = db.execute(
        "SELECT rule_type FROM rules WHERE category = 't_shirts' ORDER BY 1"
    ).fetchall()
    assert rows == [("max_length",), ("min_length",)]


@pytest.mark.parametrize("owner", ["platform", "tenant"])
def test_rules_scope_key_rejects_duplicate(db: Conn, owner: str) -> None:
    """An exact duplicate fails `rules_scope_key`, for a NULL category and, for platform rows, a
    NULL tenant too (the COALESCE key)."""
    tenant_id = f.tenant(db) if owner == "tenant" else None
    f.rule(db, tenant_id)
    expect_sqlstate(db, "23505", _insert_sql(), _params(tenant_id), constraint="rules_scope_key")
    # another version, subject, pack or tenant is another key
    f.rule(db, tenant_id, version=2)
    f.rule(db, f.tenant(db))


def _insert_sql() -> str:
    return (
        "INSERT INTO rules (tenant_id, marketplace, field, rule_type, subject, pack, rule_def) "
        "VALUES (%s, 'amazon', 'content.title', %s, '', %s, %s)"
    )


def _params(tenant_id: Any) -> tuple[Any, ...]:
    if tenant_id is None:
        body = f.rule_def("max_length", limit=200, policy_ref=f.POLICY_REF)
        return (None, "max_length", "core", Jsonb(body))
    body = f.rule_def("banned_terms", terms=["best"], pack="brand")
    return (tenant_id, "banned_terms", "brand", Jsonb(body))


def test_rules_subject_and_pack_split_the_key(db: Conn) -> None:
    """Two attribute rules on one field, or one rule in two packs, are two keys."""
    for attribute in ("fabric", "fit"):
        body = f.rule_def("required_attribute", attribute=attribute, policy_ref=f.POLICY_REF)
        f.rule(db, field="attributes", subject=attribute, rule_def=body)
    golden = f.rule_def("max_length", limit=200, pack="golden_rule", policy_ref=f.POLICY_REF)
    f.rule(db)
    f.rule(db, pack="golden_rule", rule_def=golden)


def test_rules_revision_from_sequence(db: Conn) -> None:
    first = db.execute("SELECT revision FROM rules WHERE id = %s", (f.rule(db),)).fetchone()
    second = db.execute(
        "SELECT revision FROM rules WHERE id = %s", (f.rule(db, version=2),)
    ).fetchone()
    current = db.execute("SELECT currval('rules_revision_seq')").fetchone()
    assert first is not None and second is not None
    assert second[0] > first[0]
    assert current == second
    row = db.execute(
        "SELECT pg_get_expr(d.adbin, d.adrelid), s.seqtypid::regtype::text "
        "FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
        "JOIN pg_sequence s ON s.seqrelid = 'rules_revision_seq'::regclass "
        "WHERE d.adrelid = 'rules'::regclass AND a.attname = 'revision'"
    ).fetchone()
    assert row == ("nextval('rules_revision_seq'::regclass)", "integer")


PLATFORM = f.rule_def("max_length", limit=200, policy_ref=f.POLICY_REF)
REJECTS: dict[str, tuple[dict[str, Any], bool, str]] = {
    # case: (columns, tenant row?, constraint)
    "tenant max_length": (
        {"rule_type": "max_length", "pack": "core", "rule_def": f.rule_def("max_length", limit=5)},
        True,
        "rules_tenant_types",
    ),
    "tenant regex": (
        {"rule_def": f.rule_def("banned_terms", terms=["x+"], match="regex", pack="brand")},
        True,
        "rules_platform_only_regex",
    ),
    "platform uncited": (
        {"rule_def": f.rule_def("max_length", limit=5)},
        False,
        "rules_platform_cited",
    ),
    "platform policy_ref not object": (
        {"rule_def": f.rule_def("max_length", limit=5, policy_ref="amazon")},
        False,
        "rules_platform_cited",
    ),
    "marketplace": ({"marketplace": "ebay"}, False, "ck_rules_marketplace"),
    "type mismatch": ({"rule_type": "min_length"}, False, "rules_type_matches"),
    "type missing in def": (
        {"rule_def": {k: v for k, v in PLATFORM.items() if k != "type"}},
        False,
        "rules_type_matches",
    ),
    "subject missing": (
        {
            "field": "attributes",
            "rule_type": "required_attribute",
            "rule_def": f.rule_def(
                "required_attribute", attribute="fabric", policy_ref=f.POLICY_REF
            ),
        },
        False,
        "rules_subject_matches",
    ),
    "subject on a subjectless type": ({"subject": "x"}, False, "rules_subject_matches"),
    "subject uppercase": (
        {
            "field": "attributes",
            "rule_type": "cross_field",
            "subject": "Price",
            "rule_def": f.rule_def(
                "cross_field", name="Price", left="a", op="lte", right="b", policy_ref=f.POLICY_REF
            ),
        },
        False,
        "rules_subject_matches",
    ),
    "image subject": (
        {
            "field": "images",
            "rule_type": "image_requirement",
            "subject": "main",
            "rule_def": f.rule_def(
                "image_requirement", slot="main", check="min_resolution", policy_ref=f.POLICY_REF
            ),
        },
        False,
        "rules_subject_matches",
    ),
    "pack mismatch": ({"pack": "brand"}, False, "rules_pack_matches"),
    "pack shape": (
        {"pack": "Brand", "rule_def": {**PLATFORM, "pack": "Brand"}},
        False,
        "rules_pack_matches",
    ),
    "images field, text rule": ({"field": "images"}, False, "rules_images_field"),
    "image rule, text field": (
        {
            "rule_type": "image_requirement",
            "subject": "main:format",
            "rule_def": f.rule_def(
                "image_requirement", slot="main", check="format", policy_ref=f.POLICY_REF
            ),
        },
        False,
        "rules_images_field",
    ),
    "unknown type": (
        {"rule_type": "max_words", "rule_def": {**PLATFORM, "type": "max_words"}},
        False,
        "ck_rules_rule_type",
    ),
    "def not object": (
        {"rule_type": "max_length", "rule_def": Jsonb(["max_length"])},
        False,
        "ck_rules_rule_def",
    ),
    "severity": ({"severity": "info"}, False, "ck_rules_severity"),
    "version 0": ({"version": 0}, False, "ck_rules_version"),
}


@pytest.mark.parametrize("case", sorted(REJECTS))
def test_checks_reject_rules(db: Conn, case: str) -> None:
    columns, tenant_row, constraint = REJECTS[case]
    tenant_id = f.tenant(db) if tenant_row else None
    try:
        with db.transaction():
            f.rule(db, tenant_id, **dict(columns))
    except psycopg.Error as error:
        assert error.sqlstate == "23514", error
        assert error.diag.constraint_name == constraint, error.diag.constraint_name
        return
    raise AssertionError(f"{case}: the row was accepted")


def test_rules_checks_accept_every_subject_kind(db: Conn) -> None:
    tenant_id = f.tenant(db)
    claim = f.rule_def(
        "claim_requires_attribute",
        claim="organic",
        terms=["organic"],
        requires={"attribute": "certifications"},
        pack="golden_rule",
    )
    f.rule(
        db,
        tenant_id,
        rule_type="claim_requires_attribute",
        subject="organic",
        pack="golden_rule",
        rule_def=claim,
    )
    image = f.rule_def(
        "image_requirement", slot="main", check="min_resolution", policy_ref=f.POLICY_REF
    )
    f.rule(db, field="images", subject="main:min_resolution", rule_def=image)
    cross = f.rule_def(
        "cross_field", name="price_lte_mrp", left="a", op="lte", right="b", policy_ref=f.POLICY_REF
    )
    f.rule(db, field="*", subject="price_lte_mrp", rule_def=cross)
    facts = f.rule_def(
        "fact_source", attribute="*critical", forbidden_sources=["ai"], policy_ref=f.POLICY_REF
    )
    f.rule(db, field="*", subject="*critical", rule_def=facts)
    pattern = f.rule_def(
        "pattern", name="no_emoji", regex="x", must="not_match", policy_ref=f.POLICY_REF
    )
    f.rule(db, subject="no_emoji", rule_def=pattern)
    f.rule(
        db,
        tenant_id,
        rule_def=f.rule_def("banned_terms", terms=["best"], match="word", pack="brand"),
    )


def test_rules_tenant_delete_cascades(db: Conn) -> None:
    tenant_id = f.tenant(db)
    tenant_rule = f.rule(db, tenant_id)
    platform_rule = f.rule(db)
    with as_migrator(db):  # 0004: rules are deleted only by the purge
        set_gucs(db, purge_tenant=tenant_id)
        db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
    assert db.execute("SELECT id FROM rules WHERE id = %s", (tenant_rule,)).fetchone() is None
    assert db.execute("SELECT id FROM rules WHERE id = %s", (platform_rule,)).fetchone() == (
        platform_rule,
    )


def test_rules_tenant_index_documented(db: Conn) -> None:
    rows: dict[str, str] = dict(
        db.execute(
            "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index "
            "WHERE indrelid = 'rules'::regclass"
        ).fetchall()
    )
    assert rows["rules_tenant_idx"].endswith(
        "USING btree (tenant_id, marketplace) WHERE (tenant_id IS NOT NULL)"
    )
    assert rows["rules_lookup_idx"].endswith("USING btree (marketplace, category) WHERE enabled")
    assert rows["rules_scope_key"].startswith("CREATE UNIQUE INDEX rules_scope_key")
