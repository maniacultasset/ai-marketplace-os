"""S20 T8 (AC5, AC8): invariant 2 (the adapter never contradicts the Rules Engine) and
invariant 4 (the canonical listing carries no marketplace-specific field)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import resources
from typing import Any

from hypothesis import assume, given, settings
from hypothesis import strategies as st
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.rules import RuleSet
from pydantic import TypeAdapter
from tests.adapters.amazon_support import amazon_family, amazon_item, listing, template

CATEGORIES = ("ethnic_wear_kurta_sets", "tshirts")


def _ruleset(category: str) -> RuleSet:
    from app.cli import RULES_SEED_PATH

    raw = json.loads(RULES_SEED_PATH.read_text("utf-8"))
    for row in raw:
        row.setdefault("loaded_at", datetime(2026, 10, 5, tzinfo=UTC).isoformat())
    sets = TypeAdapter(list[RuleSet]).validate_python(raw)
    (found,) = [s for s in sets if s.marketplace.value == "amazon" and s.category == category]
    return found


def _vocabulary(category: str) -> dict[str, list[Any]]:
    """Every value the engine accepts for each spec key: allowed values and value_map keys for
    an enum, a few values of the right type otherwise."""
    out: dict[str, list[Any]] = {}
    for key, spec in template(category).template.specs.items():
        if spec.value_type == "enum":
            out[key] = [*(spec.allowed_values or []), *spec.value_map]
        elif spec.value_type in ("number", "measurement"):
            out[key] = [1, 2, 180, 2.5]
        elif spec.value_type == "boolean":
            out[key] = [True, False]
        else:
            out[key] = ["Solid", "India", "Straight"]
    return out


@st.composite
def _listings(draw: st.DrawFn) -> tuple[str, CanonicalListing]:
    category = draw(st.sampled_from(CATEGORIES))
    vocab = _vocabulary(category)
    required = {"color", "fabric_type"}
    attributes = {k: draw(st.sampled_from(v)) for k, v in vocab.items() if k in required}
    optional = draw(st.sets(st.sampled_from(sorted(set(vocab) - required))))
    attributes |= {k: draw(st.sampled_from(vocab[k])) for k in optional}
    title = draw(st.text(st.characters(categories=["L", "N", "Zs"]), min_size=20, max_size=150))
    base = listing()
    body = base.model_copy(
        update={
            "category": category,
            "attributes": attributes,
            "content": base.content.model_copy(update={"title": title.strip() or "Kurta set"}),
        }
    )
    return category, CanonicalListing.model_validate(body.model_dump())


@settings(max_examples=150, deadline=None)
@given(_listings())
def test_adapter_never_contradicts_engine(case: tuple[str, CanonicalListing]) -> None:
    from app.adapters.amazon.mapping import check
    from app.services.rules_engine import evaluate

    category, body = case
    seeded = template(category)
    report = evaluate(body, _ruleset(category), template=seeded.template)
    assume(report.passed)
    assert check(amazon_item(body, template=seeded)) == []


@settings(max_examples=40, deadline=None)
@given(_listings(), st.sampled_from(["size", "color", "size_color"]))
def test_adapter_family_never_contradicts_engine(
    case: tuple[str, CanonicalListing], theme: str
) -> None:
    from app.adapters.amazon.mapping import check
    from app.services.rules_engine import evaluate

    category, body = case
    seeded = template(category)
    assume(evaluate(body, _ruleset(category), template=seeded.template).passed)
    rows = {
        "size": [{"size": "S"}, {"size": "M"}],
        "color": [{"color": "Navy"}, {"color": "maroon"}],
        "size_color": [{"size": "S", "color": "navy blue"}, {"size": "L", "color": "Black"}],
    }[theme]
    assert check(amazon_family(body, theme=theme, children=rows, template=seeded)) == []


def _amazon_only_names() -> set[str]:
    """Every Amazon wire name that is not also a neutral canonical key."""
    from app.adapters.amazon import constants as c
    from app.db.seed_templates import seed_templates

    names = {
        *c.CONTENT_FIELDS.values(),
        c.KEYWORD_ATTRIBUTE,
        c.MAIN_IMAGE,
        *c.OTHER_IMAGES,
        *c.VARIATION_ATTRIBUTES,
        "productType",
        "marketplace_id",
        "language_tag",
        "media_location",
        "parent_sku",
    }
    for seed in seed_templates():
        for key, spec in seed.template.specs.items():
            if spec.marketplace_field and spec.marketplace_field != key:
                names.add(spec.marketplace_field)
    return names


def _schema_properties(schema: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                found |= set(props)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def test_canonical_has_no_marketplace_fields() -> None:
    from app.db.seed_templates import seed_templates

    amazon = _amazon_only_names()
    assert "item_name" in amazon and "generic_keyword" in amazon
    properties = _schema_properties(CanonicalListing.model_json_schema())
    assert not properties & amazon, properties & amazon
    dictionary = json.loads(
        (resources.files("mkos_schemas") / "data/attribute_dictionary.json").read_text("utf-8")
    )
    keys = {entry["key"] for entry in dictionary["attributes"]}
    assert not keys & amazon, keys & amazon
    for seed in seed_templates():
        used = set(seed.template.specs) | set(seed.template.required_attributes)
        assert not used & amazon, used & amazon
