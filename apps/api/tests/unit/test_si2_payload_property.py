"""SI2 tester (AC7 property, PLAN §3.7): through the overlay and each adapter's real `build`, every
factual attribute the pinned template maps reaches the Amazon and Flipkart payload as the mapped
seller fact, or not at all: an AI value never does, whatever the AI and the seller give."""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

import pytest
from app.adapters.amazon import mapping as amazon_mapping
from app.adapters.base import FACT_SPEC_KEYS, AdapterTemplate
from app.adapters.flipkart import mapping as flipkart_mapping
from app.core.resolved_facts import ProductFacts, ResolvedFact
from app.services.seller_facts import FACT_ONLY_KEYS, FACTUAL_KEYS, seller_overlay
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mkos_schemas.jobs import AttributeSpec
from tests.adapters import amazon_support, flipkart_support
from tests.adapters.contract import sample_listing

_NOISE = st.text(alphabet="qxzjkv", min_size=3, max_size=8)  # never an allowed spelling


def _keys(template: AdapterTemplate) -> list[str]:
    """The factual attribute keys whose marketplace field no other spec or seller fact feeds."""
    specs = template.template.specs
    fields = Counter(str(s.marketplace_field) for s in specs.values() if s.marketplace_field)
    return sorted(
        k
        for k, s in specs.items()
        if k in FACTUAL_KEYS
        and k not in FACT_ONLY_KEYS
        and k not in FACT_SPEC_KEYS
        and s.value_type in ("enum", "text", "number")
        and fields[str(s.marketplace_field)] == 1
    )


def _value(spec: AttributeSpec) -> st.SearchStrategy[Any]:
    if spec.value_type == "number":
        return st.integers(1, 12)
    spellings = sorted({*(spec.allowed_values or ()), *spec.value_map})
    return st.one_of(st.sampled_from(spellings), _NOISE) if spellings else _NOISE


def _world(template: AdapterTemplate) -> st.SearchStrategy[tuple[dict[str, Any], dict[str, Any]]]:
    keys = _keys(template)
    specs = template.template.specs
    side = st.fixed_dictionaries({}, optional={k: _value(specs[k]) for k in keys})
    return st.tuples(side, side)


def _facts(seller: dict[str, Any]) -> ProductFacts:
    return ProductFacts(
        product_id=uuid.uuid4(),
        sku="SKU-P",
        level="standalone",
        attributes={
            k: ResolvedFact(key=k, value=v, normalized=None, source="seller")
            for k, v in seller.items()
        },
    )


def _expected(spec: AttributeSpec, value: Any, map_enum: Any) -> Any:
    if spec.value_type == "enum":
        return map_enum(spec, str(value))
    return value if spec.value_type == "number" else str(value)


MARKETPLACES = {
    "amazon": (amazon_support.template, amazon_support.amazon_item, amazon_mapping),
    "flipkart": (flipkart_support.template, flipkart_support.fk_item, flipkart_mapping),
}


@pytest.mark.parametrize("marketplace", sorted(MARKETPLACES))
@pytest.mark.parametrize("category", ["ethnic_wear_kurta_sets", "tshirts"])
def test_payload_factual_value_is_the_sellers_or_absent(marketplace: str, category: str) -> None:
    make_template, make_item, mapping = MARKETPLACES[marketplace]
    template = make_template(category)
    specs = template.template.specs
    assert _keys(template), "no factual attribute to check"

    @settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(world=_world(template))
    def check(world: tuple[dict[str, Any], dict[str, Any]]) -> None:
        ai, seller = world
        body = sample_listing().model_copy(
            update={"attributes": ai, "confidence_score": None, "attribute_sources": {}}
        )
        overlaid = seller_overlay(body, _facts(seller), template.template)
        sent = make_item(overlaid, template=template)
        items, errors = mapping.build(sent)
        (only,) = items
        attributes = only.payload["attributes"]
        for key in _keys(template):
            field = str(specs[key].marketplace_field)
            got = [e["value"] for e in attributes.get(field, [])]
            if key not in seller:
                assert got == [], (key, ai.get(key), got)
                continue
            want = _expected(specs[key], seller[key], mapping.map_enum)
            if want is None:  # unmappable seller value: refused, never replaced by the AI's
                assert got == []
                assert any(
                    e.field == f"attributes.{key}" and e.error_code.value == "VALUE_NOT_ALLOWED"
                    for e in errors
                ), (key, seller[key])
            else:
                assert got == [want], (key, seller[key], got)

    check()
