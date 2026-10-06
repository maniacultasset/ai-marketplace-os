"""S14a T8: max_items, min_items, cross_field, pattern (AC 4; D3f, D10c, D10f)."""

from __future__ import annotations

from typing import Any

import pytest
from app.services.rules_engine import evaluate
from tests.rules_engine_builders import listing, rule, ruleset


def _errors(item: Any, *rules: Any, **kw: Any) -> list[Any]:
    return list(evaluate(item, ruleset(*rules), **kw).errors)


def _items(kind: str, limit: int, field: str = "content.bullets", **kw: Any) -> Any:
    return rule(
        {"type": kind, "limit": limit, "message": "{value} items, limit {limit}", **kw}, field=field
    )


def test_rule_type_max_items_pass_fail() -> None:
    item = listing(content={"bullets": ["a", "b", "c"]})
    errs = _errors(item, _items("max_items", 2))
    assert [(e.field, e.error_code.value, e.message) for e in errs] == [
        ("content.bullets", "FIELD_TOO_LONG", "3 items, limit 2")
    ]
    assert errs[0].value_submitted == "a b c"
    assert _errors(item, _items("max_items", 3)) == []
    assert len(_errors(item, _items("max_items", 2, "content.bullets[*]"))) == 1


def test_max_items_unique_duplicate_fails() -> None:
    item = listing(keywords={"search_terms": ["Kurta", " kurta ", "set"]})
    r = _items("max_items", 10, "keywords.search_terms", unique=True)
    assert [e.field for e in _errors(item, r)] == ["keywords.search_terms"]
    assert _errors(listing(keywords={"search_terms": ["a", "b"]}), r) == []
    assert _errors(item, _items("max_items", 10, "keywords.search_terms")) == []


def test_rule_type_min_items_pass_fail() -> None:
    item = listing(content={"bullets": ["a"]})
    errs = _errors(item, _items("min_items", 2))
    assert [(e.error_code.value, e.message) for e in errs] == [
        ("FIELD_TOO_SHORT", "1 items, limit 2")
    ]
    assert _errors(item, _items("min_items", 1)) == []


def test_min_items_empty_keywords() -> None:
    r = _items("min_items", 1, "keywords.hidden_terms")
    assert [e.field for e in _errors(listing(), r)] == ["keywords.hidden_terms"]


def test_items_on_list_attribute() -> None:
    r = _items("max_items", 1, "attributes.sizes")
    assert len(_errors(listing(attributes={"sizes": ["s", "m"]}), r)) == 1


@pytest.mark.parametrize(
    ("field", "attrs"),
    [
        ("content.title", {}),
        ("*", {}),
        ("attributes.color", {"color": "black"}),
        ("facts.mrp", {}),
    ],
)
def test_count_rule_on_non_list_no_item(field: str, attrs: dict[str, Any]) -> None:
    item = listing(attributes=attrs)
    assert _errors(item, _items("max_items", 1, field)) == []
    assert _errors(item, _items("min_items", 100, field)) == []


def _cross(left: str, op: str, right: str) -> Any:
    return rule(
        {
            "type": "cross_field",
            "name": "check",
            "left": left,
            "op": op,
            "right": right,
            "message": "{value} fails",
        },
        field="attributes.price",
    )


@pytest.mark.parametrize(
    ("op", "attrs", "fails"),
    [
        ("lte", {"price": 5, "mrp": 10}, False),
        ("lte", {"price": 10, "mrp": 10}, False),
        ("lte", {"price": 11, "mrp": 10}, True),
        ("lt", {"price": 10, "mrp": 10}, True),
        ("lt", {"price": 9.5, "mrp": 10}, False),
        ("gte", {"price": 10, "mrp": 10}, False),
        ("gte", {"price": 9, "mrp": 10}, True),
        ("gt", {"price": 10, "mrp": 10}, True),
        ("gt", {"price": 11, "mrp": 10}, False),
        ("eq", {"price": "Black", "mrp": " black"}, False),
        ("eq", {"price": 3, "mrp": 3.0}, False),
        ("eq", {"price": True, "mrp": True}, False),
        ("eq", {"price": "a", "mrp": "b"}, True),
        ("ne", {"price": "a", "mrp": "b"}, False),
        ("ne", {"price": "a", "mrp": "A"}, True),
    ],
)
def test_rule_type_cross_field_pass_fail(op: str, attrs: dict[str, Any], fails: bool) -> None:
    errs = _errors(listing(attributes=attrs), _cross("attributes.price", op, "attributes.mrp"))
    assert bool(errs) is fails
    if fails:
        assert errs[0].field == "attributes.price"
        assert errs[0].error_code.value == "CROSS_FIELD_MISMATCH"


@pytest.mark.parametrize(
    ("op", "right", "fails"),
    [
        ("contains_value_of", "black", False),
        ("contains_value_of", "red", True),
        ("contains_value_of", "blac", True),
        ("not_contains_value_of", "red", False),
        ("not_contains_value_of", "BLACK", True),
    ],
)
def test_rule_type_cross_field_contains(op: str, right: str, fails: bool) -> None:
    item = listing(content={"title": "Black cotton kurta"}, attributes={"color": right})
    errs = _errors(item, _cross("content.title", op, "attributes.color"))
    assert bool(errs) is fails


def test_cross_field_contains_number_right() -> None:
    item = listing(content={"title": "Pack of 3 kurtas"}, attributes={"count": 3})
    assert _errors(item, _cross("content.title", "contains_value_of", "attributes.count")) == []


def test_cross_field_missing_side_no_item() -> None:
    r = _cross("attributes.price", "lte", "attributes.mrp")
    assert _errors(listing(attributes={"price": 5}), r) == []
    assert _errors(listing(attributes={"mrp": 5}), r) == []
    assert _errors(listing(attributes={"price": " ", "mrp": 5}), r) == []


@pytest.mark.parametrize(
    "attrs",
    [{"price": "5", "mrp": 10}, {"price": True, "mrp": 10}, {"price": 1, "mrp": True}],
)
def test_cross_field_incomparable_fails_closed(attrs: dict[str, Any]) -> None:
    for op in ("lte", "eq"):
        errs = _errors(listing(attributes=attrs), _cross("attributes.price", op, "attributes.mrp"))
        assert [e.error_code.value for e in errs] == ["CROSS_FIELD_MISMATCH"]


@pytest.mark.parametrize(
    "side", ["facts.mrp", "brand.name", "variants[a].facts.mrp", "request", "listing"]
)
def test_cross_field_not_listing_root_no_item(side: str) -> None:
    item = listing(attributes={"price": 5})
    assert _errors(item, _cross("attributes.price", "lte", side)) == []
    assert _errors(item, _cross(side, "lte", "attributes.price")) == []


def test_cross_field_list_or_dict_side_fails_closed() -> None:
    item = listing(attributes={"price": 5, "tags": ["a"]})
    assert len(_errors(item, _cross("content.bullets", "lte", "attributes.price"))) == 1
    assert len(_errors(item, _cross("attributes", "lte", "attributes.price"))) == 1
    assert (
        len(_errors(item, _cross("attributes.tags", "contains_value_of", "attributes.price"))) == 1
    )
    assert len(_errors(item, _cross("content.title", "contains_value_of", "attributes.tags"))) == 1


def _pattern(regex: str, must: str = "match", field: str = "attributes.gtin", **kw: Any) -> Any:
    return rule(
        {
            "type": "pattern",
            "name": "p",
            "regex": regex,
            "must": must,
            "message": "{value} bad",
            **kw,
        },
        field=field,
    )


@pytest.mark.parametrize("must", ["match", "not_match"])
def test_rule_type_pattern_pass_fail(must: str) -> None:
    r = _pattern(r"^\d{13}$", must)
    good = listing(attributes={"gtin": "8901234567890"})
    bad = listing(attributes={"gtin": "89012-34"})
    if must == "match":
        assert _errors(good, r) == []
        errs = _errors(bad, r)
        assert [(e.field, e.error_code.value, e.message) for e in errs] == [
            ("attributes.gtin", "PATTERN_MISMATCH", "89012-34 bad")
        ]
    else:
        assert _errors(bad, r) == []
        assert len(_errors(good, r)) == 1


def test_pattern_flag_i() -> None:
    item = listing(attributes={"gtin": "ABC"})
    assert len(_errors(item, _pattern("^abc$"))) == 1
    assert _errors(item, _pattern("^abc$", flags=["i"])) == []


def test_pattern_number_attribute_as_str() -> None:
    assert _errors(listing(attributes={"gtin": 8901234567890}), _pattern(r"^\d{13}$")) == []
    assert len(_errors(listing(attributes={"gtin": 12}), _pattern(r"^\d{13}$"))) == 1
    assert _errors(listing(attributes={"gtin": True}), _pattern(r"^\d{13}$")) == []
    assert _errors(listing(attributes={}), _pattern(r"^\d{13}$")) == []


def test_pattern_on_unindexed_list_per_item() -> None:
    item = listing(keywords={"search_terms": ["ok", "has space", "fine"]})
    errs = _errors(item, _pattern(r"^\S+$", field="keywords.search_terms"))
    assert [e.field for e in errs] == ["keywords.search_terms[1]"]
