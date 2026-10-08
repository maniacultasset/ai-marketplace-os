"""S20 T5 (D8; AC7, AC8; C12): the Amazon template data for the two launch categories."""

from __future__ import annotations

import hashlib
import json
from importlib import resources

DICTIONARY = json.loads(
    (resources.files("mkos_schemas") / "data/attribute_dictionary.json").read_text("utf-8")
)
S9_V1_SHA256 = "6e582d2dbcd740736ed635ec4c215379d4e3996364f3da1edbab3809bb617729"


def test_amazon_seed_templates_two_categories() -> None:
    from app.db.seed_templates import seed_templates

    got = [(t.template.marketplace.value, t.template.category, t.version, t.product_type) for t in seed_templates()]  # fmt: skip
    assert got == [
        ("amazon", "ethnic_wear_kurta_sets", 2, "KURTA"),
        ("amazon", "tshirts", 1, "SHIRT"),
    ]
    for seed in seed_templates():
        assert seed.template.variation_themes == [["size"], ["color"], ["size", "color"]]
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
