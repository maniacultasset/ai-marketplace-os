"""S20 T5 (D8; AC7, AC8; C12): the Amazon template data for the two launch categories."""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

DICTIONARY = json.loads(
    (resources.files("mkos_schemas") / "data/attribute_dictionary.json").read_text("utf-8")
)
FIXTURES = Path(__file__).parent / "rules_engine" / "fixtures"
S9_V1_SHA256 = "6e582d2dbcd740736ed635ec4c215379d4e3996364f3da1edbab3809bb617729"
APPAREL = [["size"], ["color"], ["size", "color"]]
THEMES = {"ethnic_wear_kurta_sets": APPAREL, "tshirts": APPAREL, "home_decor_lamps": [["color"]]}


def test_amazon_seed_templates_two_categories() -> None:
    from app.db.seed_templates import seed_templates

    got = [(t.template.marketplace.value, t.template.category, t.version, t.product_type) for t in seed_templates()]  # fmt: skip
    assert got == [
        ("amazon", "ethnic_wear_kurta_sets", 4, "KURTA"),  # SI2, feedback item 10
        ("amazon", "tshirts", 3, "SHIRT"),  # SI2, feedback item 10
        ("amazon", "home_decor_lamps", 3, "LAMP"),  # A2, feedback item 10
    ]
    for seed in seed_templates():
        assert seed.template.variation_themes == THEMES[seed.template.category]
        assert all(spec.marketplace_field for spec in seed.template.specs.values())


def test_amazon_seed_templates_every_spec_key_canonical() -> None:
    from app.db.seed_templates import seed_templates

    keys = {entry["key"] for entry in DICTIONARY["attributes"]}
    for seed in seed_templates():
        template = seed.template
        used = (
            set(template.specs)
            | set(template.required_attributes)
            | set(template.optional_attributes)
            | {axis for theme in template.variation_themes for axis in theme}
        )
        assert used <= keys, used - keys


def test_amazon_seed_templates_value_map_targets_allowed() -> None:
    from app.db.seed_templates import seed_templates

    for seed in seed_templates():
        for key, spec in seed.template.specs.items():
            if spec.allowed_values is not None:
                assert set(spec.value_map.values()) <= set(spec.allowed_values), key
            folded = [k.strip().casefold() for k in spec.value_map]
            assert len(folded) == len(set(folded)), key
            assert all(k == k.strip().casefold() for k in spec.value_map), key


def test_amazon_seed_templates_required_match_rules() -> None:
    """The adapter never requires what the seeded RuleSets do not (invariant 2)."""
    from app.cli import RULES_SEED_PATH
    from app.db.seed_templates import seed_templates

    rule_sets = json.loads(RULES_SEED_PATH.read_text("utf-8"))
    for seed in seed_templates():
        (rule_set,) = [
            r
            for r in rule_sets
            if (r["marketplace"], r["category"]) == ("amazon", seed.template.category)
        ]
        required = {
            r["rule_def"]["attribute"]
            for r in rule_set["rules"]
            if r["rule_def"]["type"] == "required_attribute"
        }
        spec_required = {k for k, s in seed.template.specs.items() if s.requirement == "required"}
        assert set(seed.template.required_attributes) | spec_required == required


def test_amazon_seed_templates_for_generation_strips_mapping() -> None:
    from app.db.seed_templates import seed_templates

    for seed in seed_templates():
        stripped = seed.template.for_generation()
        for spec in stripped.specs.values():
            assert spec.marketplace_field is None and spec.value_map == {}


def test_amazon_seed_templates_s9_v1_constants_unchanged() -> None:
    from app.db.seed_ops import _template_rows

    digest = hashlib.sha256(json.dumps(_template_rows(), sort_keys=True).encode()).hexdigest()
    assert digest == S9_V1_SHA256


# ---- S22 T15 (D7; R3): the Flipkart template data ----


def test_flipkart_seed_templates_two_categories() -> None:
    from app.db.seed_templates import flipkart_seed_templates

    got = [(t.template.marketplace.value, t.template.category, t.version, t.product_type) for t in flipkart_seed_templates()]  # fmt: skip
    assert got == [
        ("flipkart", "ethnic_wear_kurta_sets", 4, "kurta"),  # SI2, feedback item 10
        ("flipkart", "tshirts", 3, "t_shirt"),  # SI2, feedback item 10
        ("flipkart", "home_decor_lamps", 3, "table_lamp"),  # A2, feedback item 10
    ]
    for seed in flipkart_seed_templates():
        assert seed.template.variation_themes == THEMES[seed.template.category]
        assert all(spec.marketplace_field for spec in seed.template.specs.values())


def test_flipkart_seed_templates_spec_keys_canonical() -> None:
    from app.db.seed_templates import flipkart_seed_templates

    keys = {entry["key"] for entry in DICTIONARY["attributes"]}
    for seed in flipkart_seed_templates():
        template = seed.template
        used = (
            set(template.specs)
            | set(template.required_attributes)
            | set(template.optional_attributes)
            | {axis for theme in template.variation_themes for axis in theme}
        )
        assert used <= keys, used - keys


def test_flipkart_seed_templates_value_map_targets_allowed() -> None:
    from app.db.seed_templates import flipkart_seed_templates

    for seed in flipkart_seed_templates():
        for key, spec in seed.template.specs.items():
            if spec.allowed_values is not None:
                assert set(spec.value_map.values()) <= set(spec.allowed_values), key
            folded = [k.strip().casefold() for k in spec.value_map]
            assert len(folded) == len(set(folded)), key
            assert all(k == k.strip().casefold() for k in spec.value_map), key
        colour = seed.template.specs["color"]
        assert colour.marketplace_field == "Colour" and colour.value_map["navy blue"] == "Navy Blue"


def test_flipkart_seed_templates_required_match_rules() -> None:
    from app.cli import RULES_SEED_PATH
    from app.db.seed_templates import flipkart_seed_templates

    rule_sets = json.loads(RULES_SEED_PATH.read_text("utf-8"))
    for seed in flipkart_seed_templates():
        (rule_set,) = [
            r
            for r in rule_sets
            if (r["marketplace"], r["category"]) == ("flipkart", seed.template.category)
        ]
        required = {
            r["rule_def"]["attribute"]
            for r in rule_set["rules"]
            if r["rule_def"]["type"] == "required_attribute"
        }
        spec_required = {k for k, s in seed.template.specs.items() if s.requirement == "required"}
        assert set(seed.template.required_attributes) | spec_required == required


def test_flipkart_seed_templates_for_generation_strips_mapping() -> None:
    from app.db.seed_templates import flipkart_seed_templates

    for seed in flipkart_seed_templates():
        stripped = seed.template.for_generation()
        assert all(
            s.marketplace_field is None and s.value_map == {} for s in stripped.specs.values()
        )


def test_flipkart_seed_templates_s9_v1_unchanged() -> None:
    from app.db.seed_ops import _template_rows

    rows = [r for r in _template_rows() if r["marketplace"] == "flipkart"]
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    assert [r["version"] for r in rows] == [1, 1]
    assert all(r["attributes"]["specs"] == {} for r in rows)
    assert len(digest) == 64


# ---- A2 (ai-deps PLAN C1, C20): the home_decor_lamps mappings and their dictionary keys ----

LAMP_KEYS = ("lamp_type", "lamp_material", "shade_material", "light_source_type")
# The kurta and tshirts entries of `platform_template_loads()`: unchanged by A2; SI2's new
# versions (Amazon kurta v3 / tshirts v2, Flipkart kurta v3 / t_shirt v2) moved the pin once, and
# feedback item 10's (kurta v4 / tshirts v3, recommended attributes) once more.
APPAREL_TEMPLATES_SHA256 = "a640c30245d7688c1e48171015415ba0c51c45aa164d26f6de545a08b3c106db"


def test_platform_dictionary_file_loads() -> None:
    """`dictionary sync` (the prod migrate job's second step) validates the whole file first:
    a lamp key named `material` would shadow `fabric_type`'s input alias and fail it (C1)."""
    from app.db.platform_loads import load_dictionary

    loaded = load_dictionary()
    keys = {entry.key: entry for entry in loaded.attributes}
    assert "material" not in keys
    assert keys["fabric_type"].input_aliases == ["material"]
    for key in LAMP_KEYS:
        entry = keys[key]
        assert (entry.section, entry.storage, entry.value_type) == (
            "attributes",
            "attributes",
            "text",
        )
        assert not entry.is_critical and not entry.confirm_agent_captures
        assert entry.input_aliases == [] and entry.columns == [] and entry.enabled
    assert loaded.category_attributes == []  # C20: nothing reads lamp category rows yet


def _lamp(seeds: list[Any]) -> Any:
    (seed,) = [s for s in seeds if s.template.category == "home_decor_lamps"]
    return seed


def test_lamp_templates_require_the_seeded_rule_attributes() -> None:
    from app.db.seed_templates import flipkart_seed_templates, seed_templates

    for seed in (_lamp(seed_templates()), _lamp(flipkart_seed_templates())):
        template = seed.template
        assert seed.version == 3
        assert template.required_attributes == ["color", "product_dimensions"]
        assert {k for k, s in template.specs.items() if s.requirement == "required"} == {
            "color",
            "product_dimensions",
        }
        assert set(LAMP_KEYS) <= set(template.optional_attributes)
        assert set(template.optional_attributes) == set(template.specs) - {
            "color",
            "product_dimensions",
        }
        assert template.specs["color"].value_type == "enum"
        assert template.specs["product_dimensions"].value_type == "text"


def _decor_001(marketplace: str) -> Any:
    from mkos_schemas.listing import CanonicalListing

    path = FIXTURES / f"decor_001.{marketplace}.json"
    return CanonicalListing.model_validate_json(path.read_bytes())


def test_decor_001_preview_mapped_amazon() -> None:
    """Before A2 the DECOR-001 lamp previewed as CATEGORY_NOT_MAPPED on both marketplaces."""
    from app.adapters.amazon.mapping import preview
    from tests.adapters.amazon_support import amazon_item, template

    sent = amazon_item(_decor_001("amazon"), template=template("home_decor_lamps"))
    got = preview(sent)
    assert got.product_type == "LAMP"
    assert got.report.errors == []
    attributes = got.items[0].payload["attributes"]
    assert attributes["color"][0]["value"] == "Gold"
    assert attributes["item_dimensions"][0]["value"] == "30 x 15 x 15 cm"


def test_decor_001_preview_mapped_flipkart() -> None:
    from app.adapters.flipkart.mapping import preview
    from tests.adapters.flipkart_support import fk_item, template

    sent = fk_item(_decor_001("flipkart"), template=template("home_decor_lamps"))
    got = preview(sent)
    assert got.product_type == "table_lamp"
    assert got.report.errors == []


def test_apparel_templates_unchanged() -> None:
    """Kurta and tshirts load exactly what they loaded before A2 (C20)."""
    from app.db.seed_ops import platform_template_loads

    body = [
        entry.model_dump(mode="json")
        for entry in platform_template_loads()
        if entry.template.category != "home_decor_lamps"
    ]
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    assert digest == APPAREL_TEMPLATES_SHA256


# ---- SI2 T5/T6 (AC6, AC8, AC9): wider lists, value maps and the seller-fact specs ----


def _all_seeds() -> list[Any]:
    from app.db.seed_templates import flipkart_seed_templates, seed_templates

    return [*seed_templates(), *flipkart_seed_templates()]


def _apparel(marketplace: str, category: str) -> Any:
    (seed,) = [
        s
        for s in _all_seeds()
        if s.template.marketplace.value == marketplace and s.template.category == category
    ]
    return seed


@pytest.mark.parametrize(
    "seed", _all_seeds(), ids=lambda s: f"{s.template.marketplace.value}-{s.template.category}"
)
def test_si2_template_consistency(seed: Any) -> None:
    from app.services.rules_engine.template import template_allowed

    dictionary = json.loads(
        resources.files("mkos_schemas").joinpath("data/attribute_dictionary.json").read_text()
    )
    keys = {e["key"] for e in dictionary["attributes"]}
    fields = [s.marketplace_field for s in seed.template.specs.values() if s.marketplace_field]
    assert len(fields) == len(set(fields)), "two specs share a marketplace field"
    for key, spec in seed.template.specs.items():
        assert key in keys, key
        if spec.allowed_values is not None:
            assert set(spec.value_map.values()) <= set(spec.allowed_values), key
    if "color" in seed.template.specs and seed.template.specs["color"].allowed_values:
        allowed = template_allowed(seed.template, "color")
        assert allowed is not None
        assert set(seed.template.specs["color"].allowed_values) <= set(allowed)


@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
@pytest.mark.parametrize("category", ["ethnic_wear_kurta_sets", "tshirts"])
def test_si2_fact_specs_optional_and_versions(marketplace: str, category: str) -> None:
    seed = _apparel(marketplace, category)
    for key in ("fiber_composition", "manufacturer", "country_of_origin"):
        assert seed.template.specs[key].requirement == "optional"
    # SI2's versions, each one up for feedback item 10's recommended attributes
    assert seed.version == (4 if category == "ethnic_wear_kurta_sets" else 3)


def test_si2_amazon_value_maps() -> None:
    from app.adapters.amazon.mapping import map_enum

    specs = _apparel("amazon", "ethnic_wear_kurta_sets").template.specs
    assert map_enum(specs["color"], "Mustard") == "Mustard"
    assert map_enum(specs["color"], "Peach") == "Peach"
    assert map_enum(specs["color"], "off white") == "Off White"
    assert map_enum(specs["color"], "Neon Plaid") is None
    assert map_enum(specs["fabric_type"], "Cotton Lycra") == "Cotton Lycra"
    assert map_enum(specs["fabric_type"], "cotton spandex") == "Cotton Lycra"
    assert specs["fabric_type"].marketplace_field == "material"
    assert specs["fiber_composition"].marketplace_field == "fabric_type"
    country = specs["country_of_origin"]
    assert map_enum(country, "India") == "IN"
    assert map_enum(country, "IN") == "IN"
    assert map_enum(country, "bharat") == "IN"
    assert map_enum(country, "XX") is None


def test_si2_flipkart_value_maps() -> None:
    from app.adapters.amazon.mapping import map_enum

    specs = _apparel("flipkart", "tshirts").template.specs
    assert map_enum(specs["color"], "Mustard") == "Mustard"
    assert map_enum(specs["fabric_type"], "Cotton Lycra") == "Cotton Lycra"
    country = specs["country_of_origin"]
    assert map_enum(country, "IN") == "India"
    assert map_enum(country, "India") == "India"
    assert map_enum(country, "XX") is None
    assert specs["fiber_composition"].marketplace_field == "Fabric Composition"
    assert specs["manufacturer"].marketplace_field == "Manufacturer Details"


def test_si5_seed_barcodes_cover_every_sellable_sku() -> None:
    """Every sellable seeded SKU has its own valid EAN-13 per tenant; a style holds none."""
    from app.core.identifiers import check
    from app.db.seed_ops import (
        DEMO_FAMILY_COLOURS,
        DEMO_FAMILY_SIZES,
        DEMO_FAMILY_SKU,
        SEED_BARCODES,
        TENANTS,
        demo_family_child_sku,
        products_for,
    )

    for tenant in TENANTS:
        skus = {sku for sku, _, _ in products_for("demo", tenant.slug)}
        if tenant.slug == "acme":
            skus |= {
                demo_family_child_sku(s, c) for c in DEMO_FAMILY_COLOURS for s in DEMO_FAMILY_SIZES
            }
        barcodes = SEED_BARCODES[tenant.slug]
        assert set(barcodes) == skus and DEMO_FAMILY_SKU not in barcodes
        assert len(set(barcodes.values())) == len(barcodes)
        assert all(check("ean", v) is None and len(v) == 13 for v in barcodes.values())


def test_si14_lamp_templates_ship_and_their_keys_are_in_the_dictionary() -> None:
    """SI14 (AC11): the lamps category is seeded on both marketplaces; every required lamp
    attribute is a dictionary key, so the switch only has to open the category."""
    from app.db.seed_templates import flipkart_seed_templates, seed_templates

    keys = {entry["key"] for entry in DICTIONARY["attributes"]}
    lamps = [
        t.template
        for t in (*seed_templates(), *flipkart_seed_templates())
        if t.template.category == "home_decor_lamps"
    ]
    assert sorted(t.marketplace.value for t in lamps) == ["amazon", "flipkart"]
    for template in lamps:
        assert set(template.required_attributes) <= keys, template.marketplace


@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
def test_photo_and_ai_values_reach_the_kurta_fit_enum(marketplace: str) -> None:
    """Prod QA 2026-11-09 item 24: the photo read's "regular fit" was sent to Amazon, whose KURTA
    fit enum is Regular / Relaxed / Slim, and refused."""
    from importlib import import_module

    from app.services.photo_facts import VOCAB

    map_enum = import_module(f"app.adapters.{marketplace}.mapping").map_enum
    fit = _apparel(marketplace, "ethnic_wear_kurta_sets").template.specs["fit"]
    assert fit.allowed_values == ["Regular", "Relaxed", "Slim"]
    assert map_enum(fit, "regular fit") == "Regular"
    assert map_enum(fit, "Regular Fit") == "Regular"
    assert map_enum(fit, "SLIM") == "Slim"
    assert map_enum(fit, "relaxed-fit") == "Relaxed"
    assert map_enum(fit, "loose fit") is None  # not in the enum: still refused
    # every photo fit the enum has a word for maps
    for value in VOCAB["fit"]:
        assert (map_enum(fit, value) is None) == (value in {"loose fit", "oversized"}), value


def test_enum_match_needs_one_clear_match() -> None:
    from app.services.rules_engine.text import enum_match

    assert enum_match("v neck", ["Round Neck", "V-Neck"]) == "V-Neck"
    assert enum_match("round", ["Round Neck", "V-Neck"]) == "Round Neck"
    assert enum_match("slim fit", ["Slim", "Slim Fit"]) == "Slim Fit"  # exact wins
    assert enum_match("regular", ["Regular Fit", "Regular Neck"]) is None  # two answers: none
    assert enum_match("cotton", ["Cotton Lycra"]) is None


# ---- client feedback item 10: recommended attributes ----

RECOMMENDED = {
    "ethnic_wear_kurta_sets": ["fit", "kurta_type", "neckline", "pattern", "sleeve_length"],
    "tshirts": ["fit", "neckline", "pattern", "sleeve_length"],
    "home_decor_lamps": ["lamp_material", "lamp_type", "light_source_type", "shade_material"],
}


@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
@pytest.mark.parametrize("category", sorted(RECOMMENDED))
def test_recommended_attributes_are_optional_mapped_specs(marketplace: str, category: str) -> None:
    from app.services.rules_engine import requirement_levels

    (seed,) = [
        s
        for s in _all_seeds()
        if s.template.marketplace.value == marketplace and s.template.category == category
    ]
    template = seed.template
    assert template.recommended_attributes == RECOMMENDED[category]
    for key in template.recommended_attributes:
        # sent like any optional key: an optional spec the adapter maps, never required
        assert key in template.optional_attributes
        assert template.specs[key].requirement == "optional"
        assert template.specs[key].marketplace_field
    levels = requirement_levels(template)
    assert sorted(k for k, v in levels.items() if v == "recommended") == RECOMMENDED[category]
    assert {k for k, v in levels.items() if v == "required"} == set(template.required_attributes)
