"""S20 T5 (D8; AC7, AC8; C12): the Amazon template data for the two launch categories."""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from pathlib import Path
from typing import Any

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
        ("amazon", "ethnic_wear_kurta_sets", 2, "KURTA"),
        ("amazon", "tshirts", 1, "SHIRT"),
        ("amazon", "home_decor_lamps", 2, "LAMP"),  # A2
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
        ("flipkart", "ethnic_wear_kurta_sets", 2, "kurta"),
        ("flipkart", "tshirts", 1, "t_shirt"),
        ("flipkart", "home_decor_lamps", 2, "table_lamp"),  # A2
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
# The kurta and tshirts entries of `platform_template_loads()` before A2: unchanged by it.
APPAREL_TEMPLATES_SHA256 = "163538417d23d1bbcaa4b898214e3c5d2850d28ceecf1cce51ec17b150f49762"


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
        assert seed.version == 2
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
