"""Hypothesis strategies for the Rules Engine property tests (S14a CODE-PLAN T13)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast, get_args
from uuid import UUID

from hypothesis import assume
from hypothesis import strategies as st
from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.rules import Rule, RuleDef
from pydantic import ValidationError
from tests.rules_engine_builders import CATEGORY, LISTING_ID, PRODUCT_ID, TENANT_ID

REPO_ROOT = Path(__file__).resolve().parents[3]
RULEDEF_MEMBERS: tuple[Any, ...] = get_args(get_args(RuleDef)[0])

VOCAB = [
    "best",
    "#1",
    "cure",
    "organic",
    "free shipping",
    "kurta",
    "cotton",
    "Black",
    "".join(chr(0xFEE0 + ord(c)) for c in "BEST"),
    "कुर्ता",
    "number 1",
    "100%",
    "secure",
    "😀",
    "8901234567890",
]
TERMS = ["best", "#1", "cure", "organic", "free shipping", "number 1", "guaranteed", "kurta"]
SAFE_REGEXES = [r"^\d+(\.\d+)?$", r"\d+\.\d+", r"https?://.*", r"^[A-Z]{2}\d{6,}$", r"\d+%", "best"]
HOSTILE = ["{value:>999999999}", "{brand}", "{", "{value.__class__}", "{value!r}", "}{"]
MESSAGES = ["x {value}", "{term} {attribute} {limit}", "plain message", "{allowed_values}"]
OVERRIDE_CODES = ["VALUE_NOT_ALLOWED", "MISSING_ATTRIBUTE"]
ATTR_KEYS = ["color", "fabric_type", "sizes", "count", "price", "mrp", "certifications"]
SAMPLE_FIELDS = [
    "listing",
    "request",
    "file.header",
    "file.row",
    "content.title",
    "content.bullets",
    "content.bullets[1]",
    "content.description",
    "keywords.search_terms",
    "keywords.search_terms[0]",
    "keywords.hidden_terms",
    "keywords.hidden_terms[2]",
    "attributes",
    *[f"attributes.{k}" for k in ATTR_KEYS],
    "images[0]",
    "facts.mrp",
    "variants",
    "variants[a]",
    "variants[a].facts.mrp",
    "brand.name",
    "*",
    "content.bullets[*]",
    "images",
]
SIDE_PATHS = [f for f in SAMPLE_FIELDS if f not in ("*", "content.bullets[*]", "images")]
CONDITION_PATHS = [
    "content.title",
    "attributes.color",
    "attributes.count",
    "product.gender",
    "product.age",
    "account.settings.apparel",
]


def valid(model: Callable[..., Any], data: dict[str, Any]) -> Any:
    """The validated model, or None when the contract refuses the draw."""
    try:
        return model(data)
    except (ValidationError, ValueError):
        return None


def _text(max_size: int) -> st.SearchStrategy[str]:
    raw = st.text(st.characters(exclude_categories=["Cs"]), min_size=1, max_size=max_size)
    words = st.lists(st.sampled_from(VOCAB), min_size=1, max_size=6).map(" ".join)
    return st.one_of(words, raw).filter(lambda s: s.strip() != "")


_scalar = st.one_of(
    _text(40),
    st.integers(-5, 1000),
    st.floats(-10, 1000, allow_nan=False, allow_infinity=False),
    st.booleans(),
)
_attr_value = st.one_of(_scalar, st.lists(st.one_of(st.sampled_from(VOCAB), _text(20)), max_size=4))


@st.composite
def listings(draw: st.DrawFn) -> CanonicalListing:
    data = {
        "listing_id": LISTING_ID,
        "product_id": PRODUCT_ID,
        "marketplace": "amazon",
        "category": CATEGORY,
        "content": {
            "title": draw(_text(260)),
            "bullets": draw(st.lists(_text(60), min_size=1, max_size=10)),
            "description": draw(_text(300)),
        },
        "keywords": {
            "search_terms": draw(st.lists(_text(30), max_size=6)),
            "hidden_terms": draw(st.lists(_text(30), max_size=6)),
        },
        "attributes": draw(
            st.fixed_dictionaries(
                {"fabric_type": st.one_of(st.sampled_from(["nylon kurta", "cotton"]), _attr_value)},
                optional={
                    "color": st.one_of(st.sampled_from(["black", "red"]), _attr_value),
                    **dict.fromkeys(ATTR_KEYS[2:], _attr_value),
                },
            )
        ),
    }
    item: CanonicalListing | None = valid(CanonicalListing.model_validate, data)
    assume(item is not None)
    assert item is not None
    return item


_terms = st.lists(st.sampled_from(TERMS), min_size=1, max_size=4, unique=True)
_conditions = st.lists(
    st.one_of(
        st.builds(
            lambda p, op: {"path": p, "op": op},
            st.sampled_from(CONDITION_PATHS),
            st.sampled_from(["present", "absent"]),
        ),
        st.builds(
            lambda p, op, v: {"path": p, "op": op, "value": v},
            st.sampled_from(CONDITION_PATHS),
            st.sampled_from(["eq", "ne"]),
            st.one_of(st.sampled_from(VOCAB), st.integers(0, 3), st.booleans()),
        ),
        st.builds(
            lambda p, op, vs: {"path": p, "op": op, "values": vs},
            st.sampled_from(CONDITION_PATHS),
            st.sampled_from(["in", "not_in"]),
            st.lists(st.sampled_from(VOCAB), min_size=1, max_size=3),
        ),
    ),
    min_size=1,
    max_size=3,
)
_limit = st.integers(1, 300)
_unit = st.sampled_from(["chars", "bytes", "words"])
_attr = st.sampled_from(ATTR_KEYS)
_values = st.lists(st.sampled_from([*VOCAB, "3", "true"]), min_size=1, max_size=4)

GENERATORS: dict[str, st.SearchStrategy[dict[str, Any]]] = {
    "max_length": st.fixed_dictionaries({"limit": _limit, "unit": _unit}),
    "min_length": st.fixed_dictionaries({"limit": _limit, "unit": _unit}),
    "banned_terms": st.one_of(
        st.fixed_dictionaries(
            {
                "terms": _terms,
                "match": st.sampled_from(["word", "substring"]),
                "case_sensitive": st.booleans(),
            }
        ),
        st.fixed_dictionaries(
            {
                "terms": st.lists(
                    st.sampled_from(SAFE_REGEXES), min_size=1, max_size=3, unique=True
                ),
                "match": st.just("regex"),
                "case_sensitive": st.booleans(),
            }
        ),
    ),
    "required_attribute": st.fixed_dictionaries(
        {"attribute": _attr}, optional={"allowed_values": _values}
    ),
    "max_items": st.fixed_dictionaries({"limit": st.integers(1, 12), "unique": st.booleans()}),
    "min_items": st.fixed_dictionaries({"limit": st.integers(1, 12)}),
    "allowed_values": st.one_of(
        st.fixed_dictionaries({"attribute": _attr, "source": st.just("inline"), "values": _values}),
        st.fixed_dictionaries({"attribute": _attr, "source": st.just("template")}),
    ),
    "conditional_required": st.fixed_dictionaries({"attribute": _attr, "when": _conditions}),
    "cross_field": st.fixed_dictionaries(
        {
            "name": st.sampled_from(["a", "b"]),
            "left": st.sampled_from(SIDE_PATHS),
            "op": st.sampled_from(
                ["lte", "lt", "eq", "ne", "gte", "gt", "contains_value_of", "not_contains_value_of"]
            ),
            "right": st.sampled_from(SIDE_PATHS),
        }
    ),
    "claim_requires_attribute": st.fixed_dictionaries(
        {
            "claim": st.just("organic"),
            "terms": _terms,
            "match": st.sampled_from(["word", "substring"]),
            "requires": st.fixed_dictionaries(
                {"attribute": _attr},
                optional={"any_of": st.lists(st.sampled_from(VOCAB), min_size=1, max_size=3)},
            ),
        }
    ),
    "image_requirement": st.fixed_dictionaries(
        {
            "slot": st.just("main"),
            "check": st.just("min_resolution"),
            "params": st.just({"min_px": 1000}),
        }
    ),
    "pattern": st.fixed_dictionaries(
        {
            "name": st.just("p"),
            "regex": st.sampled_from(SAFE_REGEXES),
            "must": st.sampled_from(["match", "not_match"]),
            "flags": st.sampled_from([[], ["i"]]),
        }
    ),
    "fact_source": st.fixed_dictionaries(
        {"attribute": st.sampled_from(["mrp", "*critical"]), "forbidden_sources": st.just(["ai"])}
    ),
    "fact_backed_attribute": st.fixed_dictionaries({"attribute": st.just("mrp")}),
}


def _type_of(member: Any) -> str:
    return get_args(member.model_fields["type"].annotation)[0]  # type: ignore[no-any-return]


def _rule_def(member: Any) -> st.SearchStrategy[dict[str, Any]]:
    name = _type_of(member)
    base = GENERATORS.get(name, st.just({}))  # a 15th type is drawn, and refused draws filtered
    return st.builds(
        lambda body, msg, sugg, code: {
            "type": name,
            "message": msg,
            "suggestion": sugg,
            **body,
            **({} if code is None else {"error_code": code}),
        },
        base,
        st.sampled_from(MESSAGES),
        st.one_of(st.none(), st.sampled_from([*HOSTILE, "Fix {value} to {limit}"])),
        st.one_of(st.none(), st.sampled_from(OVERRIDE_CODES)),
    )


_rule_defs = st.one_of(*[_rule_def(m) for m in RULEDEF_MEMBERS])


def _make_rule(
    rule_id: UUID, field: str, rule_def: dict[str, Any], severity: str, tenant: bool, enabled: bool
) -> Rule | None:
    data = {
        "id": rule_id,
        "tenant_id": TENANT_ID if tenant else None,
        "marketplace": "amazon",
        "category": CATEGORY,
        "field": "images" if rule_def["type"] == "image_requirement" else field,
        "severity": severity,
        "enabled": enabled,
        "rule_def": rule_def,
    }
    return valid(Rule.model_validate, data)  # type: ignore[no-any-return]


def _pool() -> list[Rule]:
    raw: list[tuple[str, dict[str, Any], bool]] = [
        ("content.title", {"type": "max_length", "limit": 200}, False),
        ("content.title", {"type": "max_length", "limit": 20}, False),
        ("content.title", {"type": "min_length", "limit": 5}, False),
        ("*", {"type": "banned_terms", "terms": ["best", "#1", "cure", "number 1"]}, False),
        ("*", {"type": "banned_terms", "terms": ["kurta"], "match": "substring"}, True),
        ("content.title", {"type": "banned_terms", "terms": ["free shipping"]}, True),
        (
            "content.description",
            {"type": "banned_terms", "terms": [r"\d+%"], "match": "regex"},
            False,
        ),
        ("attributes.color", {"type": "required_attribute", "attribute": "color"}, False),
        (
            "attributes.fabric_type",
            {"type": "required_attribute", "attribute": "fabric_type"},
            False,
        ),
        (
            "attributes.fabric_type",
            {
                "type": "required_attribute",
                "attribute": "fabric_type",
                "allowed_values": ["cotton"],
            },
            False,
        ),
        ("content.bullets", {"type": "max_items", "limit": 3, "unique": True}, False),
        ("content.bullets[*]", {"type": "min_items", "limit": 2}, False),
        ("content.bullets[*]", {"type": "max_length", "limit": 30}, False),
        ("keywords.search_terms", {"type": "min_items", "limit": 1}, False),
        ("keywords.search_terms", {"type": "max_length", "limit": 50, "unit": "bytes"}, False),
        (
            "attributes.color",
            {"type": "allowed_values", "attribute": "color", "source": "template"},
            False,
        ),
        (
            "attributes.color",
            {
                "type": "allowed_values",
                "attribute": "color",
                "source": "inline",
                "values": ["black"],
            },
            False,
        ),
        (
            "attributes.sizes",
            {
                "type": "conditional_required",
                "attribute": "sizes",
                "when": [{"path": "attributes.color", "op": "present"}],
            },
            False,
        ),
        (
            "attributes.price",
            {
                "type": "cross_field",
                "name": "p",
                "left": "attributes.price",
                "op": "lte",
                "right": "attributes.mrp",
            },
            False,
        ),
        (
            "content.title",
            {
                "type": "cross_field",
                "name": "c",
                "left": "content.title",
                "op": "contains_value_of",
                "right": "attributes.color",
            },
            False,
        ),
        (
            "*",
            {
                "type": "claim_requires_attribute",
                "claim": "organic",
                "terms": ["organic"],
                "requires": {"attribute": "certifications", "any_of": ["gots"]},
            },
            True,
        ),
        (
            "attributes.count",
            {"type": "pattern", "name": "n", "regex": r"^\d+(\.\d+)?$", "must": "match"},
            False,
        ),
        (
            "content.title",
            {"type": "pattern", "name": "t", "regex": "best", "must": "not_match", "flags": ["i"]},
            False,
        ),
        ("facts.mrp", {"type": "max_length", "limit": 1}, False),
        ("brand.name", {"type": "banned_terms", "terms": ["best"]}, False),
        ("content.title", {"type": "fact_backed_attribute", "attribute": "mrp"}, False),
        (
            "images",
            {
                "type": "image_requirement",
                "slot": "main",
                "check": "min_resolution",
                "params": {"min_px": 1},
            },
            False,
        ),
        ("attributes.certifications", {"type": "banned_terms", "terms": ["cure"]}, True),
        (
            "attributes.fabric_type",
            {
                "type": "banned_terms",
                "terms": ["a", "e", "i", "o", "u", "1", "%", "k", "#"],
                "match": "substring",
                "error_code": "VALUE_NOT_ALLOWED",
            },
            True,
        ),
        (
            "attributes.color",
            {
                "type": "banned_terms",
                "terms": ["kurta", "best", "cure", "secure", "organic"],
                "error_code": "VALUE_NOT_ALLOWED",
            },
            True,
        ),
    ]
    rules: list[Rule] = []
    for i, (field, rule_def, tenant) in enumerate(raw):
        body = {
            "message": "pool {value} {term}",
            "suggestion": HOSTILE[i % len(HOSTILE)],
            **rule_def,
        }
        made = _make_rule(UUID(int=0x9000 + i), field, body, "error", tenant, True)
        assert made is not None, (field, rule_def)
        rules.append(made)
    return rules


POOL = _pool()

_drawn_rules = st.builds(
    _make_rule,
    st.uuids(),
    st.sampled_from(SAMPLE_FIELDS),
    _rule_defs,
    st.sampled_from(["error", "warning"]),
    st.booleans(),
    st.booleans(),
).filter(lambda r: r is not None)


def rules_strategy() -> st.SearchStrategy[Rule]:
    pool = st.sampled_from(POOL)
    return st.one_of(_drawn_rules, pool, pool)


def rule_lists(max_size: int = 40) -> st.SearchStrategy[list[Rule]]:
    return st.lists(rules_strategy(), min_size=1, max_size=max_size, unique_by=lambda r: r.id)


FIXTURE_TEMPLATE = json.loads(
    (REPO_ROOT / "packages/schemas/fixtures/CategoryTemplate.json").read_text(encoding="utf-8")
)
RICH_TEMPLATE = CategoryTemplate.model_validate(
    {
        "marketplace": "amazon",
        "category": CATEGORY,
        "required_attributes": ["color"],
        "conditional_attributes": ["sizes"],
        "specs": {
            "fabric_type": {
                "requirement": "required",
                "value_type": "enum",
                "allowed_values": ["cotton", "silk"],
                "value_map": {"pure cotton": "cotton"},
            },
            "sizes": {
                "requirement": "conditional",
                "value_type": "text",
                "when": [{"path": "product.gender", "op": "eq", "value": "women"}],
            },
            "color": {"requirement": "optional", "value_type": "enum", "allowed_values": ["black"]},
        },
    }
)
templates = st.sampled_from(
    [
        RICH_TEMPLATE,
        None,
        CategoryTemplate.model_validate({**FIXTURE_TEMPLATE, "category": CATEGORY}),
    ]
)
products = st.sampled_from([None, {}, {"gender": "women"}, {"gender": None, "age": 3}])
accounts = st.sampled_from([None, {"settings": {"apparel": True}}])


TENANT_POOL = [r for r in POOL if r.tenant_id is not None]


def tenant_rule_lists() -> st.SearchStrategy[list[Rule]]:
    """Every tenant rule of the pool plus drawn tenant rules."""
    drawn = cast(
        "st.SearchStrategy[Rule]",
        _drawn_rules.filter(lambda r: r is not None and r.tenant_id is not None),
    )
    extra = st.lists(drawn, max_size=6, unique_by=lambda r: r.id)
    return extra.map(lambda rules: [*TENANT_POOL, *rules])
