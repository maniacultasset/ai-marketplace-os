"""The seed's platform RuleSets (CANON-ADDENDUM.md A§5; S9 D5, D10). `*.seed.json` is seed data,
not a golden fixture: it is validated here, and the orphan checks skip the `.seed` stems."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from mkos_schemas.rules import (
    BannedTerms,
    ClaimRequiresAttribute,
    ImageRequirement,
    MaxLength,
    RequiredAttribute,
    Rule,
    RuleSet,
)
from pydantic import TypeAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SEED = FIXTURES / "rules.seed.json"
REPO_ROOT = Path(__file__).resolve().parents[3]
BANNED = {"best", "guaranteed", "free shipping", "best seller", "guaranteed cure"}
STAR_TERMS = [  # scaffold/packages/schemas/fixtures/rules.seed.json:49-51, verbatim
    "best seller",
    "bestseller",
    "#1",
    "number 1",
    "100% guaranteed",
    "free shipping",
    "cure",
    "cures",
    "doctor recommended",
    "fda approved",
    "sale",
    "discount",
    "cheapest",
]
REQUIRED = {
    "ethnic_wear_kurta_sets": {"color", "fabric_type"},
    "home_decor_lamps": {"color", "product_dimensions"},
    "tshirts": {"color", "fabric_type"},
}
S9_SCOPES = {
    (m, c) for m in ("amazon", "flipkart") for c in ("ethnic_wear_kurta_sets", "home_decor_lamps")
}


def _load() -> list[RuleSet]:
    return TypeAdapter(list[RuleSet]).validate_json(SEED.read_bytes())


def test_rules_seed_validates_as_list_of_ruleset() -> None:
    sets = _load()
    scopes = {(s.marketplace.value, s.category) for s in sets}
    assert scopes >= S9_SCOPES
    for rule_set in sets:
        assert rule_set.rules
        assert {r.marketplace for r in rule_set.rules} == {rule_set.marketplace}
        # S17b T5: the image rows are category-NULL platform defaults (every category)
        assert {r.category for r in rule_set.rules if r.field != "images"} == {rule_set.category}
        assert {r.category for r in rule_set.rules if r.field == "images"} <= {None}
    ids = [r.id for s in sets for r in s.rules]
    assert len(ids) == len(set(ids))


def test_rules_seed_platform_rows_only() -> None:
    for rule_set in _load():
        for rule in rule_set.rules:
            assert rule.tenant_id is None
            assert rule.rule_def.policy_ref is not None  # rules_platform_cited (DATABASE.md:504)


def test_rules_seed_banned_terms_listed() -> None:
    for rule_set in _load():
        banned = [
            r
            for r in rule_set.rules
            if isinstance(r.rule_def, BannedTerms) and r.field == "content.title"
        ]
        assert len(banned) == 1
        definition = banned[0].rule_def
        assert isinstance(definition, BannedTerms)
        assert set(definition.terms) == BANNED
        by_type = {
            r.rule_def.type: r.rule_def
            for r in rule_set.rules
            if r.version == 1 and r.field == "content.title"
        }
        title_max = 150 if rule_set.category == "tshirts" and _is_flipkart(rule_set) else 200
        assert by_type["max_length"].limit == title_max  # type: ignore[union-attr]
        assert by_type["min_length"].limit == 5  # type: ignore[union-attr]


def _is_flipkart(rule_set: RuleSet) -> bool:
    return rule_set.marketplace.value == "flipkart"


def _by_scope() -> dict[tuple[str, str], RuleSet]:
    return {(s.marketplace.value, s.category): s for s in _load()}


def test_rules_seed_star_banned_terms_each_ruleset() -> None:
    """S14a D12: one `*` banned_terms row per RuleSet, the scaffold list verbatim, in its own
    marketplace's wording and citation (CODE-PLAN L4)."""
    for rule_set in _load():
        star = [r for r in rule_set.rules if r.field == "*" and r.rule_def.pack == "core"]
        assert len(star) == 1, rule_set.category
        definition = star[0].rule_def
        assert isinstance(definition, BannedTerms)
        assert definition.terms == STAR_TERMS
        assert (definition.match, definition.case_sensitive) == ("word", False)
        assert definition.suggestion
        assert definition.policy_ref is not None
        name = "Flipkart" if _is_flipkart(rule_set) else "Amazon"
        assert definition.policy_ref.source == f"{rule_set.marketplace.value}_policy"
        assert name in definition.message
        other = "Amazon" if name == "Flipkart" else "Flipkart"
        assert other not in definition.message and other not in (definition.suggestion or "")
        assert star[0].category == rule_set.category


def test_rules_seed_required_attributes_per_category() -> None:
    for (_, category), rule_set in _by_scope().items():
        required = {
            r.rule_def.attribute: r
            for r in rule_set.rules
            if isinstance(r.rule_def, RequiredAttribute)
        }
        assert set(required) == REQUIRED[category], category
        for attribute, row in required.items():
            assert row.field == f"attributes.{attribute}"
            assert row.rule_def.policy_ref is not None


def test_rules_seed_flipkart_title_v2_150() -> None:
    for category in ("ethnic_wear_kurta_sets", "home_decor_lamps"):
        rule_set = _by_scope()[("flipkart", category)]
        title_max = sorted(
            (
                r
                for r in rule_set.rules
                if r.rule_def.type == "max_length" and r.field == "content.title"
            ),
            key=lambda r: r.version,
        )
        assert [(r.version, r.rule_def.limit) for r in title_max] == [(1, 200), (2, 150)]  # type: ignore[union-attr]
        v1, v2 = title_max
        assert (v1.field, v1.category, v1.rule_def.pack) == (
            v2.field,
            v2.category,
            v2.rule_def.pack,
        )
        assert v2.enabled and v2.rule_def.policy_ref is not None
    for category in ("ethnic_wear_kurta_sets", "home_decor_lamps"):
        amazon = _by_scope()[("amazon", category)]
        title_rows = [r for r in amazon.rules if r.field == "content.title"]
        assert [r.version for r in title_rows if r.rule_def.type == "max_length"] == [1]


def test_rules_seed_ids_unique() -> None:
    ids = [str(r.id) for s in _load() for r in s.rules]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("5eed0000-0000-4000-8000-") for i in ids)
    keys = [
        (r.marketplace, r.category, r.field, r.rule_def.type, r.rule_def.pack, r.version)
        for s in _load()
        for r in s.rules
        if not isinstance(r.rule_def, RequiredAttribute | ImageRequirement)
    ]
    assert len(keys) == len(set(keys))  # rules_scope_key (subject is '' for these types)


def test_rules_seed_tshirts_rulesets_present() -> None:
    scopes = _by_scope()
    for marketplace, title_max in (("amazon", 200), ("flipkart", 150)):
        rule_set = scopes[(marketplace, "tshirts")]
        rows = {(r.field, r.rule_def.type): r for r in rule_set.rules if r.rule_def.pack == "core"}
        assert rows[("content.title", "max_length")].rule_def.limit == title_max  # type: ignore[union-attr]
        assert rows[("content.title", "max_length")].version == 1
        assert rows[("content.title", "min_length")].rule_def.limit == 5  # type: ignore[union-attr]
        title_banned = rows[("content.title", "banned_terms")].rule_def
        assert isinstance(title_banned, BannedTerms) and set(title_banned.terms) == BANNED
        star = rows[("*", "banned_terms")].rule_def
        assert isinstance(star, BannedTerms) and star.terms == STAR_TERMS
        assert {
            r.rule_def.attribute
            for r in rule_set.rules
            if isinstance(r.rule_def, RequiredAttribute)
        } == {"color", "fabric_type"}
        assert all(r.tenant_id is None and r.rule_def.policy_ref for r in rule_set.rules)
    assert list(scopes)[-2:] == [("amazon", "tshirts"), ("flipkart", "tshirts")]


def test_rules_seed_canonical_json() -> None:
    dumped = TypeAdapter(list[RuleSet]).dump_python(_load(), by_alias=True, mode="json")
    assert SEED.read_text(encoding="utf-8") == json.dumps(dumped, indent=2) + "\n"


def _module(path: Path, name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_seed_stems_are_not_orphans_but_other_names_are(tmp_path: Path) -> None:
    golden_stems = _module(Path(__file__).with_name("test_golden.py"), "golden_seed").golden_stems  # type: ignore[attr-defined]
    stems = golden_stems(FIXTURES)
    assert "rules.seed" not in stems and "RuleSet" in stems
    for name in ("RuleSet.json", "rules.seed.json", "Stray.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert golden_stems(tmp_path) == {"RuleSet", "Stray"}
    module = _module(REPO_ROOT / "scripts/ci/contracts_check.py", "contracts_check_seed")
    assert module.is_seed_fixture(Path("rules.seed.json"))  # type: ignore[attr-defined]
    assert not module.is_seed_fixture(Path("Stray.json"))  # type: ignore[attr-defined]
    assert not module.is_seed_fixture(Path("seed.json"))  # type: ignore[attr-defined]


# ---- S17b T5 (PLAN D12): the image rows ----


def _image_rules() -> list[tuple[RuleSet, Rule]]:
    return [(s, r) for s in _load() for r in s.rules if r.field == "images"]


def test_rules_seed_images_validate() -> None:
    rows = _image_rules()
    assert {r.marketplace.value for _, r in rows} == {"amazon", "flipkart"}
    for _, rule in rows:
        assert isinstance(rule.rule_def, ImageRequirement)
        assert rule.rule_def.policy_ref is not None
        assert rule.category is None and rule.tenant_id is None


def test_rules_seed_images_once_per_marketplace() -> None:
    """Each marketplace's image rows sit in exactly one RuleSet, so the seed inserts them once."""
    holders: dict[str, set[str]] = {}
    for rule_set, rule in _image_rules():
        holders.setdefault(rule.marketplace.value, set()).add(rule_set.category)
    assert all(len(cats) == 1 for cats in holders.values()), holders


def test_rules_seed_images_no_scope_key_repeats() -> None:
    """The `rules_scope_key` tuple (0002_listing_pipeline.py: marketplace, category, field,
    rule_type, subject, pack, version, tenant)."""
    keys = [
        (
            r.marketplace.value,
            r.category or "",
            r.field,
            r.rule_def.type,
            (
                f"{r.rule_def.slot}:{r.rule_def.check}"
                if isinstance(r.rule_def, ImageRequirement)
                else ""
            ),
            r.rule_def.pack,
            r.version,
        )
        for _, r in _image_rules()
    ]
    assert len(keys) == len(set(keys))


# ---- A2 (ai-deps PLAN C19; spec L9224, L9774, L9840-9842): the Amazon backend-keyword budget ----

BYTE_RULE_IDS = {
    "ethnic_wear_kurta_sets": "5eed0000-0000-4000-8000-000100010007",
    "home_decor_lamps": "5eed0000-0000-4000-8000-000100020007",
    "tshirts": "5eed0000-0000-4000-8000-000100030007",
}
APPAREL_RULES_SHA256 = "8a6f6bd465dbb3b049c9e5cc741e4fe49f17349e60ab71d044db700e2507baa0"


# QA 2026-11-09: resolution on secondary and lifestyle photos (Amazon 7-10, Flipkart 8-11)
EXTRA_PHOTO_RULE_IDS = {f"5eed0000-0000-4000-8000-00070100000{n}" for n in "789a"} | {
    f"5eed0000-0000-4000-8000-00070200000{n}" for n in "89ab"
}


# Photo rules 2.1.0 (prod demo 9 Nov): new versions of the Amazon main size, shape and
# background rows and the extra-photo size rows (longest side), Amazon's main zoom warning and
# 3:4 and 10:13 shapes, and Flipkart's shape rows as warnings. Flipkart's white-background row
# is retired in place (`enabled` false, the one mutable column).
PHOTO_RULES_2_1_IDS = {
    f"5eed0000-0000-4000-8000-{n}"
    for n in (
        "000701000101",
        "00070100000d",
        "000701000102",
        "00070100000e",
        "00070100000f",
        "000701000103",
        "000701000107",
        "000701000108",
        "000701000109",
        "00070100010a",
        "000702000103",
        "000702000104",
    )
}
RETIRED_PHOTO_RULE_IDS = {"5eed0000-0000-4000-8000-000702000005"}


def test_rules_seed_count_108() -> None:
    """78 rows, plus SI8's six Amazon bullet and description limits, plus the eight
    secondary and lifestyle resolution rows (QA 2026-11-09), plus SI14's four photo QA
    warnings (text overlay and watermark, Amazon and Flipkart main), plus the twelve photo
    rules 2.1.0 rows."""
    assert sum(len(s.rules) for s in _load()) == 108
    ids = {str(r.id) for s in _load() for r in s.rules}
    assert ids >= EXTRA_PHOTO_RULE_IDS | SI14_PHOTO_QA_RULE_IDS | PHOTO_RULES_2_1_IDS
    retired = [r for s in _load() for r in s.rules if str(r.id) in RETIRED_PHOTO_RULE_IDS]
    assert [r.enabled for r in retired] == [False]


def test_rules_seed_hidden_terms_byte_rule_amazon_only() -> None:
    """One `keywords.hidden_terms` row per Amazon RuleSet: 250 UTF-8 bytes, the stored shape
    `{"type": "max_length", "limit": 250, "unit": "bytes"}` with BYTE_BUDGET_EXCEEDED. Flipkart
    has no backend keywords (spec L9840-9844), so no Flipkart row."""
    found: dict[str, Rule] = {}
    for rule_set in _load():
        rows = [r for r in rule_set.rules if r.field.startswith("keywords.")]
        if rule_set.marketplace.value == "flipkart":
            assert rows == [], rule_set.category
            continue
        (row,) = rows
        found[rule_set.category] = row
    assert {c: str(r.id) for c, r in found.items()} == BYTE_RULE_IDS
    for category, row in found.items():
        definition = row.rule_def
        assert isinstance(definition, MaxLength)
        assert row.field == "keywords.hidden_terms" and row.category == category
        assert (definition.limit, definition.unit) == (250, "bytes")
        assert definition.error_code.value == "BYTE_BUDGET_EXCEEDED"
        assert (row.severity.value, row.version, row.enabled, row.tenant_id) == (
            "error",
            1,
            True,
            None,
        )
        assert definition.pack == "core" and definition.suggestion
        assert definition.policy_ref is not None
        assert definition.policy_ref.source == "amazon_policy"
        assert "{limit}" in definition.message and "bytes" in definition.message


def test_rules_seed_apparel_unchanged_but_the_byte_rows() -> None:
    """Kurta and tshirts keep every pre-A2 row exactly; only the byte, contact and claim rows are
    new.

    The 1.2.0 `gate` key and the 2.1.0 image `params.side` key (null on every older row) are
    additive defaults and are left out of the digest; a seed row that sets one changes it.
    """
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    new_ids = (
        set(BYTE_RULE_IDS.values())
        | CONTACT_RULE_IDS
        | CLAIM_RULE_IDS
        | SI8_TEXT_RULE_IDS
        | SI14_PHOTO_QA_RULE_IDS
        | EXTRA_PHOTO_RULE_IDS
        | PHOTO_RULES_2_1_IDS
    )

    def pre_gate(rule: dict[str, Any]) -> dict[str, Any]:
        rule_def = dict(rule["rule_def"])
        if rule_def.get("gate", "unset") is None:
            del rule_def["gate"]
        params = rule_def.get("params")
        if isinstance(params, dict) and params.get("side", "unset") is None:
            rule_def["params"] = {k: v for k, v in params.items() if k != "side"}  # 2.1.0 default
        if rule["id"] in RETIRED_PHOTO_RULE_IDS:
            rule = {**rule, "enabled": True}  # retired in place; its content is unchanged
        return {**rule, "rule_def": rule_def}

    apparel = [
        {**s, "rules": [pre_gate(r) for r in s["rules"] if r["id"] not in new_ids]}
        for s in raw
        if s["category"] != "home_decor_lamps"
    ]
    digest = hashlib.sha256(json.dumps(apparel, sort_keys=True).encode()).hexdigest()
    assert digest == APPAREL_RULES_SHA256


# SI14: the photo QA warnings (Amazon 0b/0c, Flipkart 0c/0d of the image block; renumbered
# after main's QA resolution rows took Amazon 07-0a and Flipkart 08-0b)
SI14_PHOTO_QA_RULE_IDS = {
    "5eed0000-0000-4000-8000-00070100000b",
    "5eed0000-0000-4000-8000-00070100000c",
    "5eed0000-0000-4000-8000-00070200000c",
    "5eed0000-0000-4000-8000-00070200000d",
}


# SI8: the Amazon bullet count, bullet length and description length rows (kurta 1, tshirts 3)
SI8_TEXT_RULE_IDS = {
    f"5eed0000-0000-4000-8000-0001000{c}00{n}" for c in (1, 3) for n in (12, 13, 14)
}


# ---- contact details and markup: one platform `*` regex row per RuleSet (pack `contact`) ----

CONTACT_RULE_IDS = {f"5eed0000-0000-4000-8000-000{m}000{c}0008" for m in (1, 2) for c in (1, 2, 3)}


def test_rules_seed_contact_details_row_each_ruleset() -> None:
    """URLs, email addresses, phone numbers and HTML tags are BANNED_TERM errors in every
    RuleSet: one `*` banned_terms row with `match: regex`, in its own marketplace's wording."""
    found: set[str] = set()
    for rule_set in _load():
        (row,) = [r for r in rule_set.rules if r.rule_def.pack == "contact"]
        found.add(str(row.id))
        definition = row.rule_def
        assert isinstance(definition, BannedTerms)
        assert (row.field, row.category, row.tenant_id) == ("*", rule_set.category, None)
        assert (row.severity.value, row.version, row.enabled) == ("error", 1, True)
        assert definition.error_code.value == "BANNED_TERM"
        assert (definition.match, definition.case_sensitive) == ("regex", False)
        assert definition.suggestion and definition.policy_ref is not None
        name = "Flipkart" if _is_flipkart(rule_set) else "Amazon"
        assert definition.policy_ref.source == f"{rule_set.marketplace.value}_policy"
        assert name in definition.message and name in (definition.suggestion or "")
        other = "Amazon" if name == "Flipkart" else "Flipkart"
        assert other not in definition.message and other not in (definition.suggestion or "")
    assert found == CONTACT_RULE_IDS


# ---- claims needing proof: three platform `*` claim_requires_attribute rows per RuleSet ----

CLAIM_RULE_IDS = {
    f"5eed0000-0000-4000-8000-000{m}000{c}00{n}"
    for m in (1, 2)
    for c in (1, 2, 3)
    for n in ("09", "10", "11")
}
CLAIM_BACKING = {
    "claim_certification": ("certifications", {"organic", "gots", "certified", "oeko-tex"}),
    "claim_performance": ("performance_claims", {"waterproof", "anti-bacterial", "antimicrobial"}),
    "claim_handmade": ("construction", {"handmade", "handcrafted", "handloom"}),
}


def test_rules_seed_claim_rows_each_ruleset() -> None:
    """Certification, performance and handmade claims need a backing fact in every RuleSet:
    UNSUPPORTED_CLAIM errors, in the marketplace's own wording and citation."""
    found: set[str] = set()
    for rule_set in _load():
        rows = {r.rule_def.pack: r for r in rule_set.rules if r.rule_def.pack in CLAIM_BACKING}
        assert set(rows) == set(CLAIM_BACKING), rule_set.category
        name = "Flipkart" if _is_flipkart(rule_set) else "Amazon"
        for pack, row in rows.items():
            found.add(str(row.id))
            definition = row.rule_def
            assert isinstance(definition, ClaimRequiresAttribute)
            attribute, terms = CLAIM_BACKING[pack]
            assert definition.requires.attribute == attribute
            assert terms <= set(definition.terms)
            assert (definition.match, definition.error_code.value) == ("word", "UNSUPPORTED_CLAIM")
            assert (row.field, row.severity.value, row.version, row.tenant_id) == (
                "*",
                "error",
                1,
                None,
            )
            assert row.category == rule_set.category and row.enabled
            assert definition.policy_ref is not None and definition.suggestion
            assert definition.policy_ref.source == f"{rule_set.marketplace.value}_policy"
            assert name in definition.message
    assert found == CLAIM_RULE_IDS
