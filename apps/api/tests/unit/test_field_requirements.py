"""Client feedback item 16: the per-field summary of the marketplace's rules (pure part)."""

from __future__ import annotations

import json
from dataclasses import asdict

from app.api.v1.schemas.field_requirements import ListingRequirementsOut
from app.services.field_requirements import MAX_TERMS, summarise
from mkos_schemas.rules import Rule
from tests.rules_engine_builders import FIXTURES, LISTING_ID, rule, ruleset, template


def _by_path(fields: list) -> dict:  # type: ignore[type-arg]
    return {f.path: f for f in fields}


def test_lengths_counts_and_banned_words_per_field() -> None:
    fields, _ = summarise(
        ruleset(
            rule({"type": "max_length", "limit": 200}),
            rule({"type": "max_length", "limit": 180}),  # the tighter one wins
            rule({"type": "min_length", "limit": 5}),
            rule({"type": "banned_terms", "terms": ["best", "Guaranteed"], "match": "word"}),
            rule({"type": "banned_terms", "terms": ["best", "cheap"], "match": "word"}),
            rule({"type": "max_items", "limit": 5}, field="content.bullets"),
            rule({"type": "max_length", "limit": 500}, field="content.bullets[*]"),
            rule(
                {"type": "max_length", "limit": 250, "unit": "bytes"}, field="keywords.hidden_terms"
            ),
        ),
        None,
    )
    got = _by_path(fields)
    assert [f.path for f in fields] == [
        "content.title",
        "content.bullets",
        "content.bullets[*]",
        "keywords.hidden_terms",
    ]
    title = got["content.title"]
    assert (title.max_chars, title.min_chars) == (180, 5)
    assert title.banned_terms == ["best", "Guaranteed", "cheap"] and title.banned_count == 3
    assert got["content.bullets"].max_items == 5
    assert got["content.bullets[*]"].max_chars == 500
    assert got["keywords.hidden_terms"].max_bytes == 250
    assert got["keywords.hidden_terms"].max_chars is None


def test_regex_lists_become_one_plain_line_and_never_leak() -> None:
    fields, _ = summarise(
        ruleset(
            rule(
                {
                    "type": "banned_terms",
                    "terms": ["https?://"],
                    "match": "regex",
                    "pack": "contact",
                },
                field="*",
            ),
            rule(
                {
                    "type": "banned_terms",
                    "terms": ["\\bwww\\."],
                    "match": "regex",
                    "pack": "contact",
                },
                field="*",
            ),
            rule(
                {"type": "banned_terms", "terms": ["x+y"], "match": "regex", "pack": "spam"},
                field="*",
            ),
        ),
        None,
    )
    (everywhere,) = fields
    assert everywhere.path == "*"
    assert everywhere.banned_terms == [] and everywhere.banned_count == 0
    assert everywhere.rules == [
        "No links, web addresses, emails or phone numbers.",
        "Some text patterns are not allowed.",
    ]


def test_claims_patterns_and_disabled_rules() -> None:
    fields, _ = summarise(
        ruleset(
            rule(
                {
                    "type": "claim_requires_attribute",
                    "claim": "certification",
                    "terms": ["organic", "gots", "certified", "oeko-tex"],
                    "requires": {"attribute": "certification"},
                },
                field="*",
            ),
            rule(
                {
                    "type": "pattern",
                    "name": "no_all_caps",
                    "regex": "^[A-Z ]+$",
                    "must": "not_match",
                }
            ),
            rule({"type": "max_length", "limit": 10}, enabled=False),
        ),
        None,
    )
    got = _by_path(fields)
    assert got["*"].rules == [
        "Claims like “organic”, “gots”, “certified” need the certification attribute on the product."
    ]
    assert got["content.title"].rules == ["Must not contain the no all caps format."]
    assert got["content.title"].max_chars is None  # the disabled rule is off


def test_banned_words_are_capped_but_counted() -> None:
    terms = [f"word{i}" for i in range(MAX_TERMS + 5)]
    fields, _ = summarise(ruleset(rule({"type": "banned_terms", "terms": terms})), None)
    assert len(fields[0].banned_terms) == MAX_TERMS and fields[0].banned_count == MAX_TERMS + 5


def test_attributes_levels_values_and_order() -> None:
    tpl = template(
        required_attributes=["color"],
        recommended_attributes=["neck_style"],
        optional_attributes=["pattern"],
        specs={
            "fabric_type": {
                "requirement": "optional",
                "value_type": "enum",
                "allowed_values": ["Cotton", "Silk"],
            },
            "sleeve": {
                "requirement": "optional",
                "value_type": "enum",
                "value_map": {"short": "S", "full": "F"},
            },
            "length": {
                "requirement": "optional",
                "value_type": "measurement",
                "unit": "cm",
                "max_length": 40,
            },
        },
    )
    _, attributes = summarise(
        ruleset(
            rule(
                {"type": "required_attribute", "attribute": "fabric_type"},
                field="attributes.fabric_type",
            ),
            rule(
                {
                    "type": "allowed_values",
                    "attribute": "pattern",
                    "source": "inline",
                    "values": ["Solid", "Printed"],
                },
                field="attributes.pattern",
            ),
            rule({"type": "max_length", "limit": 20}, field="attributes.length"),
        ),
        tpl,
    )
    got = {a.key: a for a in attributes}
    assert [a.key for a in attributes][:2] == ["color", "fabric_type"]  # required first
    assert got["fabric_type"].level == "required"  # raised by the rule
    assert (
        got["fabric_type"].allowed_values == ["Cotton", "Silk"]
        and got["fabric_type"].allowed_count == 2
    )
    assert got["sleeve"].allowed_values == ["full", "short"]  # the value map's keys
    assert got["pattern"].allowed_values == ["Solid", "Printed"]
    assert got["neck_style"].level == "recommended"
    assert (got["length"].max_length, got["length"].unit, got["length"].value_type) == (
        20,
        "cm",
        "measurement",
    )


def test_seeded_amazon_kurta_rules_summarise() -> None:
    """The real seed: every limit the web app used to copy comes from the rules now."""
    seed = json.loads((FIXTURES / "rules.seed.json").read_text())
    group = next(
        g
        for g in seed
        if g["marketplace"] == "amazon" and g["category"] == "ethnic_wear_kurta_sets"
    )
    rules = [Rule.model_validate(r) for r in group["rules"]]
    fields, attributes = summarise(ruleset(*rules), None)
    got = _by_path(fields)
    assert got["content.title"].max_chars == 200 and got["content.title"].min_chars == 5
    assert "best" in got["content.title"].banned_terms
    assert got["content.bullets"].max_items == 5
    assert got["content.bullets[*]"].max_chars == 500
    assert got["content.description"].max_chars == 2000
    assert got["keywords.hidden_terms"].max_bytes == 250
    assert any("links" in line for line in got["*"].rules)
    assert {a.key for a in attributes if a.level == "required"} >= {"color", "fabric_type"}
    out = ListingRequirementsOut.model_validate(
        {
            "listing_id": LISTING_ID,
            "marketplace": "amazon",
            "category": "ethnic_wear_kurta_sets",
            "rules_version": 1,
            "has_template": False,
            "fields": [asdict(f) for f in fields],
            "attributes": [asdict(a) for a in attributes],
        }
    )
    assert out.fields[0].path == "content.title"
