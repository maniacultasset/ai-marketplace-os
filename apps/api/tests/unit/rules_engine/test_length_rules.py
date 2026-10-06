"""S14a T5: dispatch, ``evaluate``, length rules and the AC 1 golden (spec L6205-6216; T83)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, get_args
from uuid import UUID

import pytest
from app.services.rules_engine import RuleSetMismatchError, evaluate
from app.services.rules_engine.evaluators import DISPATCH, NOT_EVALUATED
from mkos_schemas.rules import RuleDef
from pydantic import BaseModel
from tests.rules_engine_builders import listing, rule, ruleset, template

GOLDEN = Path(__file__).parent / "golden"
GOLDEN_RULE_ID = UUID("00000014-0000-0000-0000-000000000001")


def _title_rule(limit: int = 200, **over: Any) -> Any:
    return rule(
        {
            "type": "max_length",
            "limit": limit,
            "message": "Title is {value} chars; Amazon limit is {limit}",
            "suggestion": "Shorten the title to {limit} characters or fewer",
        },
        **over,
    )


def _run(item: Any, *rules: Any, **kw: Any) -> Any:
    marketplace = item.marketplace.value
    return evaluate(item, ruleset(*rules, marketplace=marketplace), **kw)


def test_rules_title_too_long_golden() -> None:
    item = listing(content={"title": "T" * 221})
    rep = _run(item, _title_rule(id=GOLDEN_RULE_ID))
    golden = json.loads((GOLDEN / "title_too_long.json").read_text(encoding="utf-8"))
    assert rep.model_dump(mode="json") == golden
    err = golden["errors"][0]
    assert (err["error_code"], err["field"], err["source"]) == (
        "FIELD_TOO_LONG",
        "content.title",
        "rules_engine",
    )
    assert (err["retryable"], err["marketplace"], golden["error_code"]) == (
        True,
        "amazon",
        "VALIDATION_FAILED",
    )


@pytest.mark.parametrize(
    ("marketplace", "limit", "size", "fails"),
    [
        ("amazon", 200, 200, False),
        ("amazon", 200, 201, True),
        ("flipkart", 150, 150, False),
        ("flipkart", 150, 151, True),
    ],
)
def test_rules_title_boundaries(marketplace: str, limit: int, size: int, fails: bool) -> None:
    item = listing(marketplace=marketplace, content={"title": "x" * size})
    rep = _run(item, _title_rule(limit, marketplace=marketplace))
    assert (rep.status == "failed") is fails


@pytest.mark.parametrize(
    ("unit", "text", "limit"),
    [("chars", "abcdef", 5), ("bytes", "कुर्ता", 17), ("words", "a b c d", 3)],
)
def test_rule_type_max_length_pass_fail(unit: str, text: str, limit: int) -> None:
    r = rule({"type": "max_length", "limit": limit, "unit": unit})
    assert _run(listing(content={"title": text}), r).status == "failed"
    exact = rule({"type": "max_length", "limit": limit + 1, "unit": unit})
    assert _run(listing(content={"title": text}), exact).status == "passed"


@pytest.mark.parametrize(
    ("unit", "text", "limit"),
    [("chars", "abcd", 5), ("bytes", "abcd", 5), ("words", "a b", 3)],
)
def test_rule_type_min_length_pass_fail(unit: str, text: str, limit: int) -> None:
    r = rule({"type": "min_length", "limit": limit, "unit": unit})
    rep = _run(listing(content={"title": text}), r)
    assert rep.status == "failed"
    assert rep.errors[0].error_code.value == "FIELD_TOO_SHORT"
    ok = rule({"type": "min_length", "limit": limit - 1, "unit": unit})
    assert _run(listing(content={"title": text}), ok).status == "passed"


def test_length_on_bullets_star_per_bullet() -> None:
    r = rule({"type": "max_length", "limit": 3}, field="content.bullets[*]")
    rep = _run(listing(content={"bullets": ["ok", "toolong", "abc", "four"]}), r)
    assert [e.field for e in rep.errors] == ["content.bullets[1]", "content.bullets[3]"]
    assert rep.errors[0].message == "rule failed 7"
    assert rep.errors[0].value_submitted == "toolong"


def test_length_on_list_field_joined() -> None:
    r = rule({"type": "max_length", "limit": 4}, field="keywords.search_terms")
    rep = _run(listing(keywords={"search_terms": ["ab", "cd"]}), r)
    assert [e.field for e in rep.errors] == ["keywords.search_terms"]
    assert rep.errors[0].message == "rule failed 5"
    assert rep.errors[0].value_submitted == "ab cd"


def test_length_skips_non_text_attribute() -> None:
    r = rule({"type": "max_length", "limit": 1}, field="attributes.count")
    assert _run(listing(attributes={"count": 12345}), r).errors == []
    r2 = rule({"type": "max_length", "limit": 1}, field="attributes.flag")
    assert _run(listing(attributes={"flag": True}), r2).errors == []
    r3 = rule({"type": "max_length", "limit": 1}, field="attributes.color")
    assert _run(listing(), r3).errors[0].field == "attributes.color"


@pytest.mark.parametrize(
    "field",
    [
        "facts.mrp",
        "brand.name",
        "variants",
        "request",
        "file.row",
        "listing",
        "attributes",
        "images[0]",
    ],
)
def test_length_on_non_listing_root_no_item(field: str) -> None:
    r = rule({"type": "max_length", "limit": 1}, field=field)
    assert _run(listing(), r).errors == []
    r = rule({"type": "min_length", "limit": 1000}, field=field)
    assert _run(listing(), r).errors == []


def _union_types(members: tuple[Any, ...]) -> set[str]:
    return {get_args(m.model_fields["type"].annotation)[0] for m in members}


def _classified(types: set[str]) -> bool:
    return set(DISPATCH) | NOT_EVALUATED == types and set(DISPATCH).isdisjoint(NOT_EVALUATED)


RULEDEF_MEMBERS: tuple[Any, ...] = get_args(get_args(RuleDef)[0])


def test_every_ruledef_type_classified() -> None:
    types = _union_types(RULEDEF_MEMBERS)
    assert len(types) == 14
    assert _classified(types)


def test_every_ruledef_type_classified_guard_must_fail() -> None:
    class Fifteenth(BaseModel):
        type: Literal["fifteenth"]

    assert not _classified(_union_types((*RULEDEF_MEMBERS, Fifteenth)))


NOT_EVALUATED_DEFS = [
    (
        "images",
        {
            "type": "image_requirement",
            "slot": "main",
            "check": "min_resolution",
            "params": {"min_px": 1000},
        },
    ),
    ("content.title", {"type": "fact_source", "attribute": "mrp", "forbidden_sources": ["ai"]}),
    ("content.title", {"type": "fact_backed_attribute", "attribute": "*critical"}),
]


@pytest.mark.parametrize(("field", "rule_def"), NOT_EVALUATED_DEFS)
def test_not_evaluated_types_never_produce_items(field: str, rule_def: dict[str, Any]) -> None:
    r = rule(rule_def, field=field)
    assert _run(listing(), r).errors == []


def test_disabled_rule_skipped() -> None:
    r = _title_rule(5, enabled=False)
    assert _run(listing(), r).status == "passed"


@pytest.mark.parametrize(
    "case",
    ["listing_marketplace", "listing_category", "rule_marketplace", "rule_category", "template"],
)
def test_report_shape_mismatch_raises(case: str) -> None:
    item = listing()
    rules: list[Any] = [_title_rule()]
    kw: dict[str, Any] = {}
    if case == "listing_marketplace":
        item = listing(marketplace="flipkart")
    elif case == "listing_category":
        item = listing(category="tshirts")
    elif case == "rule_marketplace":
        rules = [_title_rule(marketplace="flipkart")]
    elif case == "rule_category":
        rules = [_title_rule(category="tshirts")]
    else:
        kw["template"] = template(category="tshirts")
    with pytest.raises(RuleSetMismatchError):
        evaluate(item, ruleset(*rules), **kw)
    if case == "template":
        with pytest.raises(RuleSetMismatchError):
            evaluate(item, ruleset(*rules), template=template(marketplace="flipkart"))
