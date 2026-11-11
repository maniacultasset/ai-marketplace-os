"""Client feedback item 10: recommended attributes in the category template.

A missing recommended key is a warning that never blocks; `requirement_levels` names every key's
level; `is_recommendation` and `without_recommendations` keep the warning out of the status,
issue and pipeline paths.
"""

from __future__ import annotations

from typing import Any

from app.services.rules_engine import (
    evaluate,
    is_recommendation,
    requirement_levels,
    without_recommendations,
)
from app.services.rules_engine.report import RECOMMENDED_SUGGESTION
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem
from tests.rules_engine_builders import listing, rule, ruleset, template


def _spec(requirement: str = "optional", **kw: Any) -> dict[str, Any]:
    return {"requirement": requirement, "value_type": "text", **kw}


def _report(item: Any, tpl: Any, *rules: Any, **kw: Any) -> Any:
    return evaluate(item, ruleset(*rules), template=tpl, **kw)


def test_missing_recommended_is_a_warning_and_passes() -> None:
    tpl = template(
        optional_attributes=["fit", "pattern", "neckline"],
        recommended_attributes=["pattern", "fit", "neckline"],
    )
    rep = _report(listing(attributes={"neckline": "Round Neck"}), tpl)
    assert rep.status == "passed" and rep.error_code is None and not rep.blocking
    assert [(e.field, e.error_code, e.severity, e.rule_id) for e in rep.errors] == [
        ("attributes.fit", ErrorCode.MISSING_ATTRIBUTE, Severity.WARNING, None),
        ("attributes.pattern", ErrorCode.MISSING_ATTRIBUTE, Severity.WARNING, None),
    ]
    first = rep.errors[0]
    assert first.message == "Recommended attribute fit is missing"
    assert first.suggestion == RECOMMENDED_SUGGESTION
    assert first.source is ErrorSource.RULES_ENGINE
    assert first.retryable is False
    assert all(is_recommendation(e) for e in rep.errors)


def test_present_or_blank_recommended() -> None:
    tpl = template(recommended_attributes=["fit"])
    assert _report(listing(attributes={"fit": "Slim"}), tpl).errors == []
    blank = _report(listing(attributes={"fit": "  "}), tpl).errors
    assert [e.field for e in blank] == ["attributes.fit"]
    assert _report(listing(attributes={"fit": []}), tpl).errors[0].severity is Severity.WARNING


def test_required_wins_over_recommended() -> None:
    tpl = template(
        required_attributes=["color"],
        recommended_attributes=["color", "size"],
        specs={"size": _spec("required")},
    )
    rep = _report(listing(attributes={}), tpl)
    assert rep.status == "failed"
    assert [(e.field, e.severity) for e in rep.errors] == [
        ("attributes.color", Severity.ERROR),
        ("attributes.size", Severity.ERROR),
    ]
    assert not any(is_recommendation(e) for e in rep.errors)


def test_conditional_and_unsupported_are_never_recommended() -> None:
    when = [{"path": "product.gender", "op": "eq", "value": "women"}]
    tpl = template(
        recommended_attributes=["size_chart", "care", "spare"],
        unsupported_attributes=["care"],
        specs={"size_chart": _spec("conditional", when=when), "spare": _spec("unsupported")},
    )
    # the condition does not hold: a conditional key is not required, and not recommended either
    assert _report(listing(attributes={}), tpl, product={"gender": "men"}).errors == []
    held = _report(listing(attributes={}), tpl, product={"gender": "women"})
    assert [(e.field, e.severity) for e in held.errors] == [
        ("attributes.size_chart", Severity.ERROR)
    ]


def test_platform_error_rule_suppresses_the_recommendation() -> None:
    required = rule(
        {"type": "required_attribute", "attribute": "fit", "message": "fit please"},
        field="attributes.fit",
    )
    rep = _report(listing(attributes={}), template(recommended_attributes=["fit"]), required)
    assert [(e.rule_id, e.severity) for e in rep.errors] == [(required.id, Severity.ERROR)]


def test_warning_rule_is_not_a_recommendation() -> None:
    warn = rule(
        {"type": "required_attribute", "attribute": "fit", "message": "fit helps"},
        field="attributes.fit",
        severity="warning",
    )
    rep = _report(listing(attributes={}), template(), warn)
    assert [e.severity for e in rep.errors] == [Severity.WARNING]
    assert not is_recommendation(rep.errors[0])  # it carries a rule id


def test_recommendations_sort_after_errors() -> None:
    tpl = template(required_attributes=["fabric_type"], recommended_attributes=["fit"])
    rep = _report(listing(attributes={}), tpl)
    assert [(e.field, e.severity) for e in rep.errors] == [
        ("attributes.fabric_type", Severity.ERROR),
        ("attributes.fit", Severity.WARNING),
    ]


def test_is_recommendation_needs_every_mark() -> None:
    base = {
        "field": "attributes.fit",
        "error_code": "MISSING_ATTRIBUTE",
        "severity": "warning",
        "message": "Recommended attribute fit is missing",
        "source": "rules_engine",
        "retryable": False,
    }
    assert is_recommendation(ErrorItem.model_validate(base))
    for over in (
        {"severity": "error"},
        {"error_code": "VALUE_NOT_ALLOWED"},
        {"source": "amazon_adapter"},
        {"rule_id": "00000000-0000-4000-8000-000000000001"},
    ):
        assert not is_recommendation(ErrorItem.model_validate({**base, **over}))


def test_without_recommendations_keeps_everything_else() -> None:
    tpl = template(required_attributes=["fabric_type"], recommended_attributes=["fit", "pattern"])
    rep = _report(listing(attributes={}), tpl)
    stripped = without_recommendations(rep)
    assert [e.field for e in stripped.errors] == ["attributes.fabric_type"]
    assert (stripped.status, stripped.error_code, stripped.retryable) == (
        rep.status,
        rep.error_code,
        rep.retryable,
    )
    clean = _report(listing(attributes={"fit": "Slim", "pattern": "Solid"}), tpl)
    assert without_recommendations(clean) is clean  # nothing to drop: the same report


def test_without_recommendations_on_a_passing_report() -> None:
    rep = _report(listing(attributes={}), template(recommended_attributes=["fit"]))
    stripped = without_recommendations(rep)
    assert stripped.status == "passed" and stripped.errors == []


def test_requirement_levels() -> None:
    when = [{"path": "product.gender", "op": "eq", "value": "women"}]
    tpl = template(
        required_attributes=["color"],
        optional_attributes=["fit", "pattern", "size"],
        conditional_attributes=["size_chart"],
        unsupported_attributes=["care"],
        recommended_attributes=["fit", "pattern", "color", "care", "loose"],
        specs={
            "fabric_type": _spec("required"),
            "size_chart": _spec("conditional", when=when),
            "gift": _spec("unsupported"),
            "size": _spec(),
        },
    )
    assert requirement_levels(tpl) == {
        "care": "unsupported",
        "color": "required",
        "fabric_type": "required",
        "fit": "recommended",
        "gift": "unsupported",
        "loose": "recommended",
        "pattern": "recommended",
        "size": "optional",
        "size_chart": "conditional",
    }
    assert list(requirement_levels(tpl)) == sorted(requirement_levels(tpl))


def test_requirement_levels_required_beats_conditional() -> None:
    tpl = template(required_attributes=["size"], conditional_attributes=["size"])
    assert requirement_levels(tpl) == {"size": "required"}
    assert requirement_levels(template()) == {}


def test_deterministic() -> None:
    tpl = template(recommended_attributes=["pattern", "fit", "neckline"])
    item = listing(attributes={})
    assert _report(item, tpl) == _report(item, tpl)
