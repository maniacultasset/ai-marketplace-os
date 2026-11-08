"""SI2 T3 (AC7, D5, D15): `seller_facts` and the seller-value overlay."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from importlib import resources
from typing import Any

from app.core.resolved_facts import (
    Commercial,
    Compliance,
    FamilySummary,
    PartyFacts,
    ProductFacts,
    ResolvedFact,
)
from app.services.seller_facts import (
    FACT_ONLY_KEYS,
    FACTUAL_KEYS,
    seller_facts,
    seller_overlay,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.listing import CanonicalListing
from tests.adapters.contract import sample_listing


def fact(key: str, value: Any = None, normalized: Any = None) -> ResolvedFact:
    return ResolvedFact(key=key, value=value, normalized=normalized, source="seller")


def facts(attrs: dict[str, Any] | None = None, **over: Any) -> ProductFacts:
    body: dict[str, Any] = {
        "product_id": uuid.uuid4(),
        "sku": "SKU-1",
        "level": "standalone",
        "attributes": {k: fact(k, v) for k, v in (attrs or {}).items()},
    }
    body.update(over)
    return ProductFacts(**body)


def listing(score: float | None = 0.9, **attributes: Any) -> CanonicalListing:
    return sample_listing().model_copy(
        update={"attributes": attributes, "confidence_score": score, "attribute_sources": {}}
    )


def kurta_template() -> CategoryTemplate:
    from app.db.seed_templates import seed_templates

    (seed,) = [s for s in seed_templates() if s.template.category == "ethnic_wear_kurta_sets"]
    return seed.template


def tshirt_template() -> CategoryTemplate:
    from app.db.seed_templates import seed_templates

    (seed,) = [s for s in seed_templates() if s.template.category == "tshirts"]
    return seed.template


def test_seller_values_beat_the_ai() -> None:
    out = seller_overlay(
        listing(fabric_type="Silk", color="Navy"),
        facts({"fabric_type": "Cotton Lycra", "color": "Mustard"}),
        None,
    )
    assert out.attributes == {"fabric_type": "Cotton Lycra", "color": "Mustard"}
    assert out.attribute_sources == {"fabric_type": "fabric_type", "color": "color"}


def test_ai_factual_value_without_seller_fact_is_dropped() -> None:
    out = seller_overlay(listing(gsm="180", pattern="solid"), facts(), None)
    assert out.attributes == {"pattern": "solid"}


def test_fact_only_keys_never_stay_in_attributes() -> None:
    out = seller_overlay(
        listing(country_of_origin="CN", mrp="10", manufacturer="X", fiber_composition="x"),
        facts({"fiber_composition": "100% Cotton"}),
        None,
    )
    assert out.attributes == {}


def test_template_named_key_is_added_from_the_seller() -> None:
    out = seller_overlay(listing(), facts({"fabric_type": "Cotton"}), kurta_template())
    assert out.attributes["fabric_type"] == "Cotton"


def test_non_factual_keys_are_kept() -> None:
    out = seller_overlay(listing(pattern="solid", neckline="round"), facts(), kurta_template())
    assert out.attributes == {"pattern": "solid", "neckline": "round"}


def test_style_axes_are_left_alone() -> None:
    style = facts(level="style", family=FamilySummary(theme="size_color"))
    out = seller_overlay(listing(color="Navy", size="M"), style, None)
    assert out.attributes == {"color": "Navy", "size": "M"}


def test_ai_colour_confidence_rule() -> None:
    assert seller_overlay(listing(0.70, color="Navy"), facts(), None).attributes == {
        "color": "Navy"
    }
    assert seller_overlay(listing(0.69, color="Navy"), facts(), None).attributes == {}
    assert seller_overlay(listing(None, color="Navy"), facts(), None).attributes == {}
    seller = seller_overlay(listing(0.99, color="Navy"), facts({"color": "Mustard"}), None)
    assert seller.attributes == {"color": "Mustard"}


def test_product_weight_from_the_column() -> None:
    out = seller_overlay(
        listing(product_weight=999), facts(product_weight_g=180), tshirt_template()
    )
    assert out.attributes["product_weight"] == 180
    gone = seller_overlay(listing(product_weight=999), facts(), tshirt_template())
    assert "product_weight" not in gone.attributes


def test_object_and_blank_seller_values_are_dropped() -> None:
    out = seller_overlay(
        listing(size_measurements="x", fabric_type="Silk"),
        facts({"size_measurements": {"chest": 40}, "fabric_type": "  "}),
        None,
    )
    assert out.attributes == {}


def test_unicode_seller_value_kept_exactly() -> None:
    out = seller_overlay(listing(fabric_type="Silk"), facts({"fabric_type": "रेशम"}), None)
    assert out.attributes == {"fabric_type": "रेशम"}


def test_seller_spelling_beats_normalized() -> None:
    body = facts()
    body = body.model_copy(
        update={"attributes": {"color": fact("color", "Mustard Yellow", "mustard")}}
    )
    assert seller_overlay(listing(), body, kurta_template()).attributes["color"] == "Mustard Yellow"


def test_overlay_is_idempotent() -> None:
    f = facts({"fabric_type": "Cotton"})
    once = seller_overlay(listing(0.8, color="Navy", gsm="1", pattern="p"), f, kurta_template())
    assert seller_overlay(once, f, kurta_template()) == once


def test_factual_keys_drift_guard() -> None:
    raw = json.loads(
        resources.files("mkos_schemas").joinpath("data/attribute_dictionary.json").read_text()
    )
    critical = {e["key"] for e in raw["attributes"] if e.get("is_critical")}
    assert critical | {"color", "size"} == FACTUAL_KEYS
    assert {"fabric_type", "fiber_composition", "mrp", "manufacturer", "color", "size"} <= (
        FACTUAL_KEYS
    )
    assert "product_weight" not in FACT_ONLY_KEYS
    assert {"mrp", "selling_price", "manufacturer", "country_of_origin"} <= FACT_ONLY_KEYS


# ---- seller_facts ----


def test_seller_facts_money_parties_currency() -> None:
    out = seller_facts(
        facts(
            commercial=Commercial(mrp="1.10", selling_price="1.00", currency="INR"),
            compliance=Compliance(
                country_of_origin="IN",
                manufacturer=PartyFacts(legal_name="Brand Mills", address="1 Road, Surat"),
            ),
        )
    )
    assert out.mrp == Decimal("1.10") and out.selling_price == Decimal("1.00")
    assert out.currency == "INR" and out.country_of_origin == "IN"
    assert out.manufacturer_name == "Brand Mills"
    assert out.manufacturer_address == "1 Road, Surat"


def test_seller_facts_nameless_party() -> None:
    out = seller_facts(
        facts(compliance=Compliance(manufacturer=PartyFacts(legal_name=" ", address="x")))
    )
    assert out.manufacturer_name is None and out.manufacturer_address is None


def test_seller_facts_si1_composition() -> None:
    from app.adapters.base import composition_text

    stored = [{"fibre": "cotton", "percent": 60}, {"fibre": "polyester", "percent": 40}]
    body = facts().model_copy(
        update={
            "attributes": {
                "fiber_composition": fact("fiber_composition", "60% Cotton, 40% Polyester", stored)
            }
        }
    )
    out = seller_facts(body)
    assert out.fiber_composition_text == "60% Cotton, 40% Polyester"
    assert out.fiber_composition_parts == stored
    text = composition_text(out.fiber_composition_text, out.fiber_composition_parts)
    assert text == "60% Cotton, 40% Polyester"


def test_seller_facts_legacy_list_value() -> None:
    parts = [{"fiber": "cotton", "percent": 100}]
    body = facts().model_copy(
        update={"attributes": {"fiber_composition": fact("fiber_composition", parts)}}
    )
    out = seller_facts(body)
    assert out.fiber_composition_text is None and out.fiber_composition_parts == parts


# ---- AC7 property ----

_FREE = ("pattern", "neckline", "occasion")
_KEYS = sorted(FACTUAL_KEYS) + list(_FREE)
_VALUES = st.one_of(st.text(min_size=1, max_size=8), st.integers(0, 500))


@settings(max_examples=60, deadline=None)
@given(
    ai=st.dictionaries(st.sampled_from(_KEYS), _VALUES, max_size=10),
    seller=st.dictionaries(st.sampled_from(sorted(FACTUAL_KEYS)), _VALUES, max_size=10),
)
def test_every_factual_key_is_the_sellers_or_absent(
    ai: dict[str, Any], seller: dict[str, Any]
) -> None:
    out = seller_overlay(listing(None, **ai), facts(seller), kurta_template())
    for key, value in out.attributes.items():
        if key in FACTUAL_KEYS:
            assert key not in FACT_ONLY_KEYS
            assert seller.get(key) == value
