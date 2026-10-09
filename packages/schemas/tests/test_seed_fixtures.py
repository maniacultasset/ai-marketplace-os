"""The seed's platform RuleSets (CANON-ADDENDUM.md A§5; S9 D5, D10). `*.seed.json` is seed data,
not a golden fixture: it is validated here, and the orphan checks skip the `.seed` stems."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from mkos_schemas.rules import BannedTerms, ImageRequirement, RequiredAttribute, Rule, RuleSet
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
        by_type = {r.rule_def.type: r.rule_def for r in rule_set.rules if r.version == 1}
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
        star = [r for r in rule_set.rules if r.field == "*"]
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
            (r for r in rule_set.rules if r.rule_def.type == "max_length"),
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
        assert [r.version for r in amazon.rules if r.rule_def.type == "max_length"] == [1]


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
        rows = {(r.field, r.rule_def.type): r for r in rule_set.rules}
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
