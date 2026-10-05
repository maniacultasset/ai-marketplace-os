"""S14a T7: required_attribute, allowed_values, conditional_required (AC 3, AC 4; D10b/d/e)."""

from __future__ import annotations

from typing import Any

import pytest
from app.services.rules_engine import evaluate
from pydantic import ValidationError
from tests.rules_engine_builders import listing, rule, ruleset, template


def _errors(item: Any, *rules: Any, **kw: Any) -> list[Any]:
    return list(evaluate(item, ruleset(*rules), **kw).errors)


def _required(attribute: str = "fabric_type", **kw: Any) -> Any:
    return rule(
        {
            "type": "required_attribute",
            "attribute": attribute,
            "message": "{attribute} missing",
            **kw,
        },
        field=f"attributes.{attribute}",
    )


def test_required_attribute() -> None:
    rep = evaluate(listing(attributes={"color": "black"}), ruleset(_required()))
    assert rep.status == "failed"
    assert rep.retryable is False
    (err,) = rep.errors
    assert (err.field, err.error_code.value, err.source.value) == (
        "attributes.fabric_type",
        "MISSING_ATTRIBUTE",
        "rules_engine",
    )
    assert (err.retry_class.value if err.retry_class else None, err.retryable) == (
        "user_action",
        False,
    )
    assert err.message == "fabric_type missing"
    assert err.value_submitted is None


@pytest.mark.parametrize("value", ["", "  ", []])
def test_required_attribute_blank_and_empty_list(value: object) -> None:
    item = listing(attributes={"fabric_type": value})
    assert [e.error_code.value for e in _errors(item, _required())] == ["MISSING_ATTRIBUTE"]


def test_required_attribute_false_and_zero_present() -> None:
    assert _errors(listing(attributes={"fabric_type": False}), _required()) == []
    assert _errors(listing(attributes={"fabric_type": 0}), _required()) == []


def test_required_attribute_none_refused_by_contract() -> None:
    with pytest.raises(ValidationError):
        listing(attributes={"fabric_type": None})


def test_required_attribute_allowed_values_mismatch() -> None:
    r = _required(allowed_values=["cotton", "silk"])
    errs = _errors(listing(attributes={"fabric_type": "polyester"}), r)
    assert [(e.error_code.value, e.rule_id) for e in errs] == [("VALUE_NOT_ALLOWED", r.id)]
    assert _errors(listing(attributes={"fabric_type": " Cotton "}), r) == []


def _allowed(**kw: Any) -> Any:
    return rule(
        {
            "type": "allowed_values",
            "attribute": "color",
            "message": "{value} not in {allowed_values}",
            **kw,
        },
        field="attributes.color",
    )


def test_rule_type_allowed_values_inline_pass_fail() -> None:
    r = _allowed(source="inline", values=["black", "white"])
    assert _errors(listing(), r) == []
    errs = _errors(listing(attributes={"color": "red"}), r)
    assert [(e.field, e.error_code.value, e.message) for e in errs] == [
        ("attributes.color", "VALUE_NOT_ALLOWED", "red not in black, white")
    ]


def test_allowed_values_casefold_trim() -> None:
    r = _allowed(source="inline", values=["Black"])
    assert _errors(listing(attributes={"color": "  BLACK "}), r) == []


def test_allowed_values_list_value() -> None:
    r = _allowed(source="inline", values=["black", "white"])
    assert _errors(listing(attributes={"color": ["black", "WHITE"]}), r) == []
    errs = _errors(listing(attributes={"color": ["black", "red"]}), r)
    assert [e.value_submitted for e in errs] == ["black red"]


def test_allowed_values_absent_no_item() -> None:
    r = _allowed(source="inline", values=["black"])
    assert _errors(listing(attributes={}), r) == []
    assert _errors(listing(attributes={"color": " "}), r) == []


def test_allowed_values_inline_without_values_no_item() -> None:
    assert _errors(listing(attributes={"color": "red"}), _allowed(source="inline")) == []


def test_allowed_values_number_and_bool_values() -> None:
    r = _allowed(source="inline", values=["3", "true"])
    assert _errors(listing(attributes={"color": 3}), r) == []
    assert _errors(listing(attributes={"color": True}), r) == []
    assert len(_errors(listing(attributes={"color": 4}), r)) == 1


def _tpl(**spec: Any) -> Any:
    return template(specs={"color": {"requirement": "optional", "value_type": "enum", **spec}})


def test_rule_type_allowed_values_template_pass_fail() -> None:
    r = _allowed(source="template")
    tpl = _tpl(allowed_values=["Black"], value_map={"jet black": "Black"})
    assert _errors(listing(attributes={"color": "black"}), r, template=tpl) == []
    assert _errors(listing(attributes={"color": "Jet Black"}), r, template=tpl) == []
    errs = _errors(listing(attributes={"color": "red"}), r, template=tpl)
    assert [(e.rule_id, e.error_code.value) for e in errs if e.rule_id] == [
        (r.id, "VALUE_NOT_ALLOWED")
    ]


def test_allowed_values_template_missing_no_item() -> None:
    r = _allowed(source="template")
    item = listing(attributes={"color": "red"})
    assert _errors(item, r) == []
    assert _errors(item, r, template=template()) == []
    assert _errors(item, r, template=_tpl()) == []


def _conditional(*when: dict[str, Any]) -> Any:
    return rule(
        {
            "type": "conditional_required",
            "attribute": "size_chart",
            "when": list(when),
            "message": "{attribute} needed",
        },
        field="attributes.size_chart",
    )


@pytest.mark.parametrize("source", ["listing", "product", "account"])
def test_rule_type_conditional_required_pass_fail(source: str) -> None:
    path = {
        "listing": "attributes.color",
        "product": "product.gender",
        "account": "account.settings.apparel",
    }[source]
    r = _conditional({"path": path, "op": "present"})
    product = {"gender": "women"}
    account = {"settings": {"apparel": True}}
    errs = _errors(listing(), r, product=product, account=account)
    assert [(e.field, e.error_code.value) for e in errs] == [
        ("attributes.size_chart", "MISSING_ATTRIBUTE")
    ]
    assert (
        _errors(
            listing(attributes={"color": "x", "size_chart": "yes"}),
            r,
            product=product,
            account=account,
        )
        == []
    )
    assert _errors(listing(attributes={}), r, product={}, account={}) == []


def test_conditional_required_all_conditions_and() -> None:
    r = _conditional(
        {"path": "product.gender", "op": "eq", "value": "women"},
        {"path": "attributes.color", "op": "in", "values": ["black"]},
    )
    assert len(_errors(listing(), r, product={"gender": "Women"})) == 1
    assert _errors(listing(), r, product={"gender": "men"}) == []
    assert _errors(listing(attributes={"color": "red"}), r, product={"gender": "women"}) == []


def test_conditional_required_none_in_product_is_absent() -> None:
    r = _conditional({"path": "product.gender", "op": "absent"})
    assert len(_errors(listing(), r, product={"gender": None})) == 1
    r2 = _conditional({"path": "product.gender", "op": "eq", "value": "x"})
    assert _errors(listing(), r2, product={"gender": None}) == []
