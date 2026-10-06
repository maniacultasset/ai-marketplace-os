"""S14a T9: ``claim_requires_attribute`` (AC 4; D10a; T48)."""

from __future__ import annotations

from typing import Any

from app.services.rules_engine import evaluate
from tests.rules_engine_builders import TENANT_ID, listing, rule, ruleset


def _claim(any_of: list[str] | None = None, field: str = "*", **over: Any) -> Any:
    requires: dict[str, Any] = {"attribute": "certifications"}
    if any_of is not None:
        requires["any_of"] = any_of
    match = over.pop("match", "word")
    return rule(
        {
            "type": "claim_requires_attribute",
            "claim": "organic",
            "terms": ["organic", "bio"],
            "match": match,
            "requires": requires,
            "message": "{term} needs {attribute}",
        },
        field=field,
        **over,
    )


def _errors(item: Any, *rules: Any) -> list[Any]:
    return list(evaluate(item, ruleset(*rules)).errors)


ORGANIC = {"title": "Organic cotton kurta", "description": "Bio and organic cotton"}


def test_rule_type_claim_requires_attribute_pass_fail() -> None:
    errs = _errors(listing(content=ORGANIC), _claim())
    assert [(e.field, e.error_code.value, e.message) for e in errs] == [
        ("content.title", "UNSUPPORTED_CLAIM", "organic needs certifications"),
        ("content.description", "UNSUPPORTED_CLAIM", "organic, bio needs certifications"),
    ]
    item = listing(content=ORGANIC, attributes={"certifications": ["gots"]})
    assert _errors(item, _claim()) == []


def test_claim_any_of_pass_fail() -> None:
    ok = listing(content=ORGANIC, attributes={"certifications": ["gots"]})
    assert _errors(ok, _claim(["gots"])) == []
    bad = listing(content=ORGANIC, attributes={"certifications": ["oeko_tex"]})
    assert len(_errors(bad, _claim(["gots"]))) == 2
    scalar = listing(content=ORGANIC, attributes={"certifications": "gots"})
    assert _errors(scalar, _claim(["gots"])) == []


def test_claim_any_of_casefold() -> None:
    item = listing(content=ORGANIC, attributes={"certifications": [" GOTS "]})
    assert _errors(item, _claim(["gots"])) == []


def test_claim_absent_term_no_item() -> None:
    assert _errors(listing(), _claim()) == []
    assert _errors(listing(content={"title": "Inorganically dyed"}), _claim()) == []


def test_claim_substring_mode() -> None:
    item = listing(content={"title": "Inorganically dyed"})
    errs = _errors(item, _claim(match="substring", field="content.title"))
    assert [e.field for e in errs] == ["content.title"]


def test_claim_tenant_rule_runs() -> None:
    r = _claim(tenant_id=TENANT_ID, field="content.title")
    errs = _errors(listing(content=ORGANIC), r)
    assert [e.rule_id for e in errs] == [r.id]
