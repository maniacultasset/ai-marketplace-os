"""S14a T11: ``resolve_ruleset`` (AC 7; DATABASE.md:507; A§17; T55; KNOWN-ISSUES.md:24)."""

from __future__ import annotations

import random
from typing import Any
from uuid import UUID

import pytest
from app.services.rules_engine import RuleRow, RuleSetMismatchError, resolve_ruleset
from app.services.rules_engine.resolver import subject_of
from mkos_schemas.enums import Marketplace
from pydantic import ValidationError
from tests.rules_engine_builders import CATEGORY, LOADED_AT, TENANT_ID, rule

OTHER_TENANT = UUID("00000000-0000-0000-0000-0000000000dd")


def _title(limit: int, version: int = 1, **over: Any) -> Any:
    return rule({"type": "max_length", "limit": limit}, version=version, **over)


def _resolve(rows: list[RuleRow], **kw: Any) -> Any:
    args: dict[str, Any] = {
        "marketplace": Marketplace.AMAZON,
        "category": CATEGORY,
        "tenant_id": TENANT_ID,
        "loaded_at": LOADED_AT,
    }
    args.update(kw)
    return resolve_ruleset(rows, **args)


def _rows(*rules: Any) -> list[RuleRow]:
    return [RuleRow(r, i + 1) for i, r in enumerate(rules)]


def _limits(rs: Any) -> list[int]:
    return [r.rule_def.limit for r in rs.rules]


def test_resolver_highest_enabled_version_wins() -> None:
    rs = _resolve(_rows(_title(200, 1), _title(150, 2), _title(180, 3, enabled=True)))
    assert _limits(rs) == [180]


def test_resolver_disable_newest_falls_back() -> None:
    rs = _resolve(_rows(_title(200, 1), _title(150, 2, enabled=False)))
    assert _limits(rs) == [200]


def test_resolver_none_enabled_rule_off() -> None:
    rs = _resolve(_rows(_title(200, 1, enabled=False), _title(150, 2, enabled=False)))
    assert rs.rules == []
    assert rs.rules_version == 2


def test_resolver_category_shadows_null() -> None:
    rs = _resolve(_rows(_title(200, 5, category=None), _title(150, 1)))
    assert _limits(rs) == [150]


def test_resolver_category_all_disabled_uses_null() -> None:
    rs = _resolve(_rows(_title(200, 1, category=None), _title(150, 2, enabled=False)))
    assert _limits(rs) == [200]


def test_resolver_tenant_never_shadows_platform() -> None:
    platform = _title(200, 1)
    tenant_banned = rule({"type": "banned_terms", "terms": ["x"]}, tenant_id=TENANT_ID, version=9)
    platform_banned = rule({"type": "banned_terms", "terms": ["y"]})
    rs = _resolve(_rows(platform, tenant_banned, platform_banned))
    assert {r.id for r in rs.rules} == {platform.id, tenant_banned.id, platform_banned.id}
    assert rs.rules[-1].id == tenant_banned.id


def test_resolver_other_tenant_raises() -> None:
    other = rule({"type": "banned_terms", "terms": ["x"]}, tenant_id=OTHER_TENANT)
    with pytest.raises(RuleSetMismatchError):
        _resolve(_rows(other))
    with pytest.raises(RuleSetMismatchError):
        _resolve(
            _rows(rule({"type": "banned_terms", "terms": ["x"]}, tenant_id=TENANT_ID)),
            tenant_id=None,
        )


@pytest.mark.parametrize("case", ["marketplace", "category"])
def test_resolver_other_marketplace_or_category_raises(case: str) -> None:
    bad = (
        _title(200, marketplace="flipkart")
        if case == "marketplace"
        else _title(200, category="tshirts")
    )
    with pytest.raises(RuleSetMismatchError):
        _resolve(_rows(bad))


def test_resolver_rules_version_counts_disabled_rows() -> None:
    rows = [RuleRow(_title(200, 1), 3), RuleRow(_title(150, 2, enabled=False), 11)]
    rs = _resolve(rows)
    assert _limits(rs) == [200]
    assert rs.rules_version == 11


def test_resolver_rules_version_counts_dictionary() -> None:
    rs = _resolve(_rows(_title(200)), dictionary_revisions=[40, 7])
    assert rs.rules_version == 40


def test_resolver_empty_is_zero() -> None:
    rs = _resolve([])
    assert (rs.rules, rs.rules_version) == ([], 0)


def test_resolver_subject_splits_groups() -> None:
    color = rule({"type": "required_attribute", "attribute": "color"}, field="attributes.color")
    color2 = rule(
        {"type": "required_attribute", "attribute": "fabric_type"}, field="attributes.color"
    )
    params = {"min_px": 1000}
    img1 = rule(
        {"type": "image_requirement", "slot": "main", "check": "min_resolution", "params": params},
        field="images",
    )
    img2 = rule(
        {
            "type": "image_requirement",
            "slot": "main",
            "check": "background",
            "params": {"rgb": [255, 255, 255]},
        },
        field="images",
    )
    rs = _resolve(_rows(color, color2, img1, img2))
    assert {r.id for r in rs.rules} == {color.id, color2.id, img1.id, img2.id}


def test_resolver_pack_splits_groups() -> None:
    a = rule({"type": "banned_terms", "terms": ["x"]})
    b = rule({"type": "banned_terms", "terms": ["y"], "pack": "legal_metrology"})
    assert len(_resolve(_rows(a, b)).rules) == 2


def test_resolver_output_order_canonical() -> None:
    rules = [
        _title(200),
        rule({"type": "banned_terms", "terms": ["x"]}, field="*"),
        rule({"type": "banned_terms", "terms": ["x"]}, tenant_id=TENANT_ID),
        rule({"type": "required_attribute", "attribute": "color"}, field="attributes.color"),
        rule({"type": "min_length", "limit": 5}),
    ]
    base = _resolve(_rows(*rules)).model_dump_json()
    rng = random.Random(1)  # noqa: S311  # test shuffle only
    for _ in range(5):
        shuffled = list(_rows(*rules))
        rng.shuffle(shuffled)
        assert _resolve(shuffled).model_dump_json() == base


def test_resolver_over_500_raises() -> None:
    rules = [
        rule({"type": "banned_terms", "terms": [f"t{i}"], "pack": "core"}, field=f"attributes.a{i}")
        for i in range(501)
    ]
    with pytest.raises(ValidationError):
        _resolve(_rows(*rules))


def test_resolver_negative_revision_raises() -> None:
    with pytest.raises(ValueError, match="revision"):
        RuleRow(_title(200), -1)
    assert RuleRow(_title(200), 0).revision == 0


@pytest.mark.parametrize(
    ("rule_def", "subject"),
    [
        ({"type": "max_length", "limit": 1}, ""),
        ({"type": "required_attribute", "attribute": "color"}, "color"),
        ({"type": "allowed_values", "attribute": "color", "source": "template"}, "color"),
        (
            {
                "type": "conditional_required",
                "attribute": "color",
                "when": [{"path": "product.g", "op": "present"}],
            },
            "color",
        ),
        ({"type": "fact_source", "attribute": "mrp", "forbidden_sources": []}, "mrp"),
        ({"type": "fact_backed_attribute", "attribute": "*critical"}, "*critical"),
        (
            {
                "type": "claim_requires_attribute",
                "claim": "organic",
                "terms": ["organic"],
                "requires": {"attribute": "c"},
            },
            "organic",
        ),
        (
            {
                "type": "cross_field",
                "name": "price_lte_mrp",
                "left": "attributes.p",
                "op": "lte",
                "right": "facts.mrp",
            },
            "price_lte_mrp",
        ),
        ({"type": "pattern", "name": "gtin", "regex": "^x$", "must": "match"}, "gtin"),
    ],
)
def test_subject_of_matches_database_table(rule_def: dict[str, Any], subject: str) -> None:
    assert subject_of(rule(rule_def).rule_def) == subject
