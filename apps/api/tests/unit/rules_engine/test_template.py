"""S14a T10: template checks derived from the category template (AC 6; DATABASE.md:526)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.rules_engine import evaluate
from mkos_schemas.jobs import CategoryTemplate
from tests.rules_engine_builders import TENANT_ID, listing, rule, ruleset, template

FIXTURE = Path(__file__).resolve().parents[5] / "packages/schemas/fixtures/CategoryTemplate.json"


def _errors(item: Any, tpl: Any, *rules: Any, **kw: Any) -> list[Any]:
    return list(evaluate(item, ruleset(*rules), template=tpl, **kw).errors)


def _spec(requirement: str = "optional", **kw: Any) -> dict[str, Any]:
    return {"requirement": requirement, "value_type": "text", **kw}


def test_template_checks_required_missing() -> None:
    tpl = template(required_attributes=["fabric_type", "color"], specs={"size": _spec("required")})
    item = listing(attributes={"color": "black"})
    errs = _errors(item, tpl)
    assert [(e.field, e.error_code.value, e.message) for e in errs] == [
        (
            "attributes.fabric_type",
            "MISSING_ATTRIBUTE",
            "Required attribute fabric_type is missing",
        ),
        ("attributes.size", "MISSING_ATTRIBUTE", "Required attribute size is missing"),
    ]


def test_template_checks_conditional_when_true_false() -> None:
    when = [{"path": "product.gender", "op": "eq", "value": "women"}]
    tpl = template(specs={"size_chart": _spec("conditional", when=when)})
    assert [e.field for e in _errors(listing(), tpl, product={"gender": "women"})] == [
        "attributes.size_chart"
    ]
    assert _errors(listing(), tpl, product={"gender": "men"}) == []
    present = listing(attributes={"size_chart": "yes"})
    assert _errors(present, tpl, product={"gender": "women"}) == []


def test_template_checks_conditional_empty_when_skipped() -> None:
    tpl = template(conditional_attributes=["size_chart"], specs={"x": _spec("conditional")})
    assert _errors(listing(), tpl) == []


def _enum(**kw: Any) -> Any:
    return template(specs={"fabric_type": {"requirement": "optional", "value_type": "enum", **kw}})


def test_template_checks_enum_value_map_key_allowed() -> None:
    tpl = _enum(allowed_values=["Cotton"], value_map={"pure cotton": "Cotton"})
    assert _errors(listing(attributes={"fabric_type": "Pure Cotton"}), tpl) == []
    assert _errors(listing(attributes={"fabric_type": ["cotton"]}), tpl) == []
    assert _errors(listing(attributes={}), tpl) == []


def test_template_checks_enum_outside_fails() -> None:
    tpl = _enum(allowed_values=["Cotton", "Silk"])
    errs = _errors(listing(attributes={"fabric_type": "nylon"}), tpl)
    assert [(e.field, e.error_code.value, e.message) for e in errs] == [
        (
            "attributes.fabric_type",
            "VALUE_NOT_ALLOWED",
            "Value nylon is not allowed for fabric_type; allowed: Cotton, Silk",
        )
    ]
    assert _errors(listing(attributes={"fabric_type": "nylon"}), _enum()) == []


def test_template_checks_rule_id_none() -> None:
    errs = _errors(listing(attributes={}), template(required_attributes=["color"]))
    assert [e.rule_id for e in errs] == [None]
    assert errs[0].retryable is False


def test_template_checks_rule_item_wins() -> None:
    r = rule(
        {"type": "required_attribute", "attribute": "color", "message": "color please"},
        field="attributes.color",
    )
    errs = _errors(listing(attributes={}), template(required_attributes=["color"]), r)
    assert [(e.rule_id, e.message) for e in errs] == [(r.id, "color please")]


def test_template_checks_tenant_rule_does_not_suppress() -> None:
    tenant_warning = rule(
        {"type": "banned_terms", "terms": ["nylon"], "error_code": "VALUE_NOT_ALLOWED"},
        field="attributes.fabric_type",
        tenant_id=TENANT_ID,
        severity="warning",
    )
    tpl = _enum(allowed_values=["Cotton"])
    rep = evaluate(
        listing(attributes={"fabric_type": "nylon"}), ruleset(tenant_warning), template=tpl
    )
    assert rep.status == "failed"
    assert sorted(str(e.rule_id) for e in rep.errors) == sorted(["None", str(tenant_warning.id)])


def test_template_checks_none_without_template() -> None:
    assert evaluate(listing(attributes={}), ruleset()).errors == []


def test_template_fixture_validates_and_runs() -> None:
    tpl = CategoryTemplate.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
    item = listing(category=tpl.category)
    rep = evaluate(item, ruleset(category=tpl.category), template=tpl)
    assert rep.status == "passed"
