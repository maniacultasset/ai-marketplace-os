from types import SimpleNamespace

from tests.fakes import seeded_rules

from mkos_pipeline.product import banned_terms, compact_rules, max_length_for
from mkos_pipeline.schema_compat import rule_def_map, rule_limit, rule_type_of


def test_compact_rules_reads_v12_dict_defs() -> None:
    rules = seeded_rules("amazon", "ethnic_wear_kurta_sets", 80)
    compact = compact_rules(rules)
    assert compact["max_lengths"]["content.title"] == 80
    assert "sale" in compact["banned_terms"]
    assert max_length_for(rules, "content.title", 10) == 80
    assert "sale" in banned_terms(rules)


def test_rule_accessors_read_v20_model_defs() -> None:
    rule = SimpleNamespace(
        rule_type=None,
        rule_def=SimpleNamespace(
            type="max_length",
            limit=120,
            model_dump=lambda: {"type": "max_length", "limit": 120},
        ),
    )
    assert rule_type_of(rule) == "max_length"
    assert rule_def_map(rule)["limit"] == 120
    assert rule_limit(rule, "max_length") == 120
