"""Every placeholder an attribute, claim and cross-field finding fills, and the comparisons the
cross-field and claim checks make at their edges (S26 T23 gate-fix; AC 11; report.py placeholder
list, DATABASE.md:510)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.services.rules_engine import evaluate
from app.services.rules_engine.evaluators import _cross_holds
from app.services.rules_engine.text import scalar_equal
from tests.rules_engine_builders import listing, rule, ruleset

ALL = "v={value} a={attribute} ok={allowed_values}"


def _errors(item: Any, *rules: Any) -> list[Any]:
    return list(evaluate(item, ruleset(*rules)).errors)


def test_required_attribute_not_allowed_fills_every_placeholder() -> None:
    r = rule(
        {
            "type": "required_attribute",
            "attribute": "fabric_type",
            "allowed_values": ["cotton", "silk"],
            "message": ALL,
        },
        field="attributes.fabric_type",
    )
    (err,) = _errors(listing(attributes={"fabric_type": "nylon"}), r)
    assert (err.field, err.error_code.value) == ("attributes.fabric_type", "VALUE_NOT_ALLOWED")
    assert err.message == "v=nylon a=fabric_type ok=cotton, silk"
    assert err.value_submitted == "nylon"


def test_allowed_values_fills_every_placeholder() -> None:
    r = rule(
        {
            "type": "allowed_values",
            "attribute": "color",
            "source": "inline",
            "values": ["red", "blue"],
            "message": ALL,
        },
        field="attributes.color",
    )
    (err,) = _errors(listing(attributes={"color": "teal"}), r)
    assert err.message == "v=teal a=color ok=red, blue"
    assert err.value_submitted == "teal"


def test_conditional_required_fills_the_attribute() -> None:
    r = rule(
        {
            "type": "conditional_required",
            "attribute": "size_chart",
            "when": [{"path": "attributes.color", "op": "present"}],
            "message": "a={attribute}",
        },
        field="attributes.size_chart",
    )
    (err,) = _errors(listing(attributes={"color": "red"}), r)
    assert err.message == "a=size_chart"


def test_claim_with_an_empty_attribute_is_unsupported() -> None:
    r = rule(
        {
            "type": "claim_requires_attribute",
            "claim": "organic",
            "terms": ["organic"],
            "match": "word",
            "requires": {"attribute": "certifications"},
            "message": "{term} needs {attribute}",
        },
        field="content.title",
    )
    item = listing(content={"title": "Organic kurta"}, attributes={"certifications": []})
    (err,) = _errors(item, r)
    assert err.message == "organic needs certifications"


@pytest.mark.parametrize(
    ("op", "left", "right", "holds"),
    [
        ("ne", 1, 2, True),
        ("ne", 1, 1, False),
        ("eq", 1, 1.0, True),
        # incomparable sides fail closed for eq and ne alike
        ("ne", 1, "1", False),
        ("ne", "a", 1, False),
        ("ne", True, "x", False),
        ("ne", True, 1, False),
        ("eq", True, True, True),
        ("ne", "Red ", "red", False),
        ("contains_value_of", "bestseller kurta", "best", False),
        ("contains_value_of", "best kurta", "BEST", True),
        ("not_contains_value_of", "best kurta", "best", False),
        ("contains_value_of", "size 42 kurta", 42, True),
        ("contains_value_of", "best kurta", True, False),
        ("lt", 1, 2, True),
        ("lt", "1", 2, False),
    ],
)
def test_cross_holds(op: str, left: object, right: object, holds: bool) -> None:
    assert _cross_holds(op, left, right) is holds


def test_scalar_equal_needs_two_numbers() -> None:
    assert scalar_equal(1, 1.0)
    assert not scalar_equal(1, Decimal(1))
    assert not scalar_equal(Decimal(1), 1)
