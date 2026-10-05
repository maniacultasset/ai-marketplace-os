"""RuleDef, Rule, and RuleSet."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast, get_args
from uuid import UUID

import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace
from mkos_schemas.rules import (
    PLACEHOLDERS,
    AllowedValues,
    BannedTerms,
    ClaimRequiresAttribute,
    ConditionalRequired,
    CrossField,
    FactBackedAttribute,
    FactSource,
    ImageRequirement,
    MaxItems,
    MaxLength,
    MinItems,
    MinLength,
    Pattern,
    PolicyRef,
    RequiredAttribute,
    Rule,
    RuleDef,
    RuleSet,
)
from pydantic import BaseModel, TypeAdapter, ValidationError

from . import canon_sources

_RID = UUID("11111111-1111-1111-1111-111111111111")
_TS = datetime(2026, 1, 1, tzinfo=UTC)
_RULE_DEF_ADAPTER: TypeAdapter[RuleDef] = TypeAdapter(RuleDef)


def _rule(**overrides: object) -> Rule:
    base = {
        "id": _RID,
        "marketplace": Marketplace.AMAZON,
        "field": "content.title",
        "rule_def": {"type": "max_length", "message": "too long {limit}", "limit": 200},
    }
    base.update(overrides)
    return Rule.model_validate(base)


def test_ruledef_discriminator() -> None:
    schema = json.dumps(Rule.model_json_schema())
    for member in (
        "max_length",
        "min_length",
        "banned_terms",
        "required_attribute",
        "max_items",
        "min_items",
        "allowed_values",
        "conditional_required",
        "cross_field",
        "claim_requires_attribute",
        "image_requirement",
        "pattern",
        "fact_source",
        "fact_backed_attribute",
    ):
        assert member in schema
    with pytest.raises(ValidationError) as exc:
        _RULE_DEF_ADAPTER.validate_python({"message": "x", "limit": 1})
    assert any(e["type"] == "union_tag_not_found" for e in exc.value.errors())


def test_ruledef_members_match_sources() -> None:

    a16 = (Path(__file__).resolve().parents[3] / "docs/canon/CANON-ADDENDUM.md").read_text(
        encoding="utf-8"
    )
    idx = a16.find("## 16. RuleSet")
    section = a16[idx : idx + 800]
    canon_five = {
        "max_length",
        "min_length",
        "banned_terms",
        "required_attribute",
        "max_items",
    }
    for name in canon_five:
        assert name in section
    db = (Path(__file__).resolve().parents[3] / "docs/architecture/DATABASE.md").read_text(
        encoding="utf-8"
    )
    for name in (
        "min_items",
        "allowed_values",
        "conditional_required",
        "cross_field",
        "claim_requires_attribute",
        "image_requirement",
        "pattern",
        "fact_source",
        "fact_backed_attribute",
    ):
        assert f"`{name}`" in db
    members = {c.__name__ for c in get_args(get_args(RuleDef)[0]) if inspect.isclass(c)}
    expected = {
        "MaxLength",
        "MinLength",
        "BannedTerms",
        "RequiredAttribute",
        "MaxItems",
        "MinItems",
        "AllowedValues",
        "ConditionalRequired",
        "CrossField",
        "ClaimRequiresAttribute",
        "ImageRequirement",
        "Pattern",
        "FactSource",
        "FactBackedAttribute",
    }
    assert members == expected


def test_ruledef_default_error_codes() -> None:
    cases: list[tuple[type[BaseModel], ErrorCode, dict[str, Any]]] = [
        (MaxLength, ErrorCode.FIELD_TOO_LONG, {"limit": 10}),
        (MinLength, ErrorCode.FIELD_TOO_SHORT, {"limit": 1}),
        (BannedTerms, ErrorCode.BANNED_TERM, {"terms": ["bad"]}),
        (RequiredAttribute, ErrorCode.MISSING_ATTRIBUTE, {"attribute": "size"}),
        (MaxItems, ErrorCode.FIELD_TOO_LONG, {"limit": 5}),
        (MinItems, ErrorCode.FIELD_TOO_SHORT, {"limit": 1}),
        (AllowedValues, ErrorCode.VALUE_NOT_ALLOWED, {"attribute": "color", "source": "inline"}),
        (
            ConditionalRequired,
            ErrorCode.MISSING_ATTRIBUTE,
            {"attribute": "size", "when": [{"path": "attributes.color", "op": "present"}]},
        ),
        (
            CrossField,
            ErrorCode.CROSS_FIELD_MISMATCH,
            {"name": "mrp", "left": "facts.mrp", "op": "lte", "right": "attributes.price"},
        ),
        (
            ClaimRequiresAttribute,
            ErrorCode.UNSUPPORTED_CLAIM,
            {"claim": "cotton", "terms": ["cotton"], "requires": {"material": True}},
        ),
        (
            ImageRequirement,
            ErrorCode.IMAGE_NON_COMPLIANT,
            {
                "slot": "main",
                "check": "min_resolution",
                "params": {"min_px": 1000},
            },
        ),
        (Pattern, ErrorCode.PATTERN_MISMATCH, {"name": "sku", "regex": "^A+$", "must": "match"}),
        (
            FactSource,
            ErrorCode.FACT_SOURCE_FORBIDDEN,
            {"attribute": "mrp", "forbidden_sources": ["agent"]},
        ),
        (
            FactBackedAttribute,
            ErrorCode.FACT_MISMATCH,
            {"attribute": "price"},
        ),
    ]
    type_literals: dict[type[BaseModel], str] = {
        MaxLength: "max_length",
        MinLength: "min_length",
        BannedTerms: "banned_terms",
        RequiredAttribute: "required_attribute",
        MaxItems: "max_items",
        MinItems: "min_items",
        AllowedValues: "allowed_values",
        ConditionalRequired: "conditional_required",
        CrossField: "cross_field",
        ClaimRequiresAttribute: "claim_requires_attribute",
        ImageRequirement: "image_requirement",
        Pattern: "pattern",
        FactSource: "fact_source",
        FactBackedAttribute: "fact_backed_attribute",
    }
    for cls, code, extra in cases:
        payload: dict[str, Any] = {"type": type_literals[cls], "message": "m", **extra}
        obj = cls.model_validate(payload)
        assert obj.model_dump()["error_code"] is code


def test_rule_fields_match_a16() -> None:
    names = set(Rule.model_fields)
    for req in (
        "id",
        "tenant_id",
        "marketplace",
        "category",
        "field",
        "severity",
        "version",
        "enabled",
        "rule_def",
    ):
        assert req in names


def test_ruleset_fields_match_a16() -> None:
    names = set(RuleSet.model_fields)
    for req in ("marketplace", "category", "rules", "rules_version", "loaded_at"):
        assert req in names
    assert "version" not in names


def test_rule_forbidden_spellings_absent() -> None:
    for name in ("field_path", "kind", "params"):
        assert name not in Rule.model_fields
        assert name not in RuleSet.model_fields
    assert "version" not in RuleSet.model_fields


def test_params_field_only_on_image_requirement() -> None:
    for cls in get_args(get_args(RuleDef)[0]):
        if not inspect.isclass(cls):
            continue
        model = cast(type[BaseModel], cls)
        has_params = "params" in model.model_fields
        assert has_params == (cls is ImageRequirement)


def test_ruleset_docstring_is_the_addendum_text() -> None:
    block = canon_sources.addendum_block("17").strip()
    assert block.startswith('"""') and block.endswith('"""')
    expected = " ".join(block.strip('"').split())
    assert " ".join((RuleSet.__doc__ or "").split()) == expected


def test_ruleset_docstring() -> None:
    doc = " ".join((RuleSet.__doc__ or "").split())
    assert "Rows group by" in doc
    # A§17 keeps everything but its shadowing sentence (Decision 11).
    assert "PipelineDeps has NO load_rules port" in doc
    assert "the AI package fetches nothing" in doc
    assert "category rule shadows" in doc
    assert "a tenant rule shadows" not in doc
    assert "never shadow" in doc


def test_rule_message_placeholders_documented() -> None:
    with pytest.raises(ValidationError):
        MaxLength.model_validate({"type": "max_length", "message": "bad {price}", "limit": 1})
    with pytest.raises(ValidationError):
        MaxLength.model_validate({"type": "max_length", "message": "{", "limit": 1})
    MaxLength.model_validate(
        {"type": "max_length", "message": "allowed {allowed_values}", "limit": 1}
    )
    assert (
        frozenset({"value", "limit", "term", "attribute", "allowed_values", "slot", "threshold"})
        == PLACEHOLDERS
    )


def test_rule_field_vocabulary() -> None:
    img = {
        "type": "image_requirement",
        "message": "img",
        "slot": "main",
        "check": "format",
        "params": {"min_px": 100},
    }
    _rule(field="images", rule_def=img)
    with pytest.raises(ValidationError):
        _rule(field="content.title", rule_def=img)
    with pytest.raises(ValidationError):
        _rule(field="images", rule_def={"type": "max_length", "message": "m", "limit": 1})


def test_condition_operator_value_rules() -> None:
    from mkos_schemas.rules import Condition

    Condition.model_validate({"path": "attributes.color", "op": "in", "values": ["red"]})
    Condition.model_validate({"path": "attributes.color", "op": "eq", "value": "red"})
    Condition.model_validate({"path": "attributes.color", "op": "present"})
    with pytest.raises(ValidationError):
        Condition.model_validate({"path": "attributes.color", "op": "in"})
    with pytest.raises(ValidationError):
        Condition.model_validate({"path": "attributes.color", "op": "eq"})
    with pytest.raises(ValidationError):
        ConditionalRequired.model_validate(
            {
                "type": "conditional_required",
                "message": "m",
                "attribute": "size",
                "when": [],
            }
        )
    with pytest.raises(ValidationError):
        ConditionalRequired.model_validate(
            {
                "type": "conditional_required",
                "message": "m",
                "attribute": "size",
                "when": [{"path": "attributes.color", "op": "present"}] * 5,
            }
        )


def test_pattern_regex_length_cap() -> None:
    Pattern.model_validate(
        {"type": "pattern", "message": "m", "name": "n", "regex": "a" * 256, "must": "match"}
    )
    with pytest.raises(ValidationError):
        Pattern.model_validate(
            {"type": "pattern", "message": "m", "name": "n", "regex": "a" * 257, "must": "match"}
        )


def test_pattern_regex_no_nested_quantifiers() -> None:
    with pytest.raises(ValidationError):
        Pattern.model_validate(
            {"type": "pattern", "message": "m", "name": "n", "regex": "(a+)+", "must": "match"}
        )


def test_banned_terms_caps() -> None:
    BannedTerms.model_validate({"type": "banned_terms", "message": "m", "terms": ["x"] * 500})
    with pytest.raises(ValidationError):
        BannedTerms.model_validate({"type": "banned_terms", "message": "m", "terms": ["x"] * 501})
    with pytest.raises(ValidationError):
        BannedTerms.model_validate({"type": "banned_terms", "message": "m", "terms": [" "]})


def test_rule_suggestion_max_1000() -> None:
    MaxLength.model_validate(
        {
            "type": "max_length",
            "message": "m",
            "limit": 1,
            "suggestion": "s" * 1000,
        }
    )
    with pytest.raises(ValidationError):
        MaxLength.model_validate(
            {
                "type": "max_length",
                "message": "m",
                "limit": 1,
                "suggestion": "s" * 1001,
            }
        )


def test_ruleset_rules_count_cap() -> None:
    one = _rule()
    RuleSet.model_validate(
        {
            "marketplace": "amazon",
            "category": "apparel",
            "rules": [one] * 500,
            "loaded_at": _TS,
        }
    )
    with pytest.raises(ValidationError):
        RuleSet.model_validate(
            {
                "marketplace": "amazon",
                "category": "apparel",
                "rules": [one] * 501,
                "loaded_at": _TS,
            }
        )


def test_ruleset_version_and_loaded_at() -> None:
    with pytest.raises(ValidationError):
        RuleSet.model_validate(
            {
                "marketplace": "amazon",
                "category": "apparel",
                "loaded_at": datetime(2026, 1, 1),
            }
        )


def test_policy_ref_source_vocabulary() -> None:
    PolicyRef.model_validate({"source": "platform", "title": "t", "retrieved_on": date(2026, 1, 1)})
    with pytest.raises(ValidationError):
        PolicyRef.model_validate({"source": "unknown", "title": "t"})


def test_rule_category_slug_or_null() -> None:
    _rule(category="apparel")
    _rule(category=None)
    with pytest.raises(ValidationError):
        _rule(category="Bad-Category")


def test_rule_base_pack_defaults_core() -> None:
    obj = MaxLength.model_validate({"type": "max_length", "message": "m", "limit": 1})
    assert obj.pack == "core"
    MaxLength.model_validate(
        {"type": "max_length", "message": "m", "limit": 1, "pack": "brand_tone"}
    )
    with pytest.raises(ValidationError):
        MaxLength.model_validate({"type": "max_length", "message": "m", "limit": 1, "pack": "Bad"})


# Round 1 R1-5: cross_field and fact_* paths are checked against the vocabulary.
@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("__class__.__init__.__globals__", "attributes.price"),
        ("facts.mrp", "content.titel"),
        ("model_config", "attributes.price"),
    ],
)
def test_cross_field_paths_use_the_field_vocabulary(left: str, right: str) -> None:
    with pytest.raises(ValidationError):
        CrossField.model_validate(
            {
                "type": "cross_field",
                "message": "m",
                "name": "n",
                "left": left,
                "op": "lte",
                "right": right,
            }
        )


@pytest.mark.parametrize("cls", [FactSource, FactBackedAttribute])
def test_fact_rule_attribute_is_a_key_or_critical(cls: type[BaseModel]) -> None:
    tag = "fact_source" if cls is FactSource else "fact_backed_attribute"
    extra: dict[str, Any] = {"forbidden_sources": ["agent"]} if cls is FactSource else {}
    for good in ("mrp", "*critical"):
        cls.model_validate({"type": tag, "message": "m", "attribute": good, **extra})
    for bad in ("facts.mrp", "content.title", "*", "MRP"):
        with pytest.raises(ValidationError):
            cls.model_validate({"type": tag, "message": "m", "attribute": bad, **extra})
