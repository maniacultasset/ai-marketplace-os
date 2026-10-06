"""The seed's platform RuleSets (CANON-ADDENDUM.md A§5; S9 D5, D10). `*.seed.json` is seed data,
not a golden fixture: it is validated here, and the orphan checks skip the `.seed` stems."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from mkos_schemas.rules import BannedTerms, RuleSet
from pydantic import TypeAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SEED = FIXTURES / "rules.seed.json"
REPO_ROOT = Path(__file__).resolve().parents[3]
BANNED = {"best", "guaranteed", "free shipping", "best seller", "guaranteed cure"}
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
        assert {r.category for r in rule_set.rules} == {rule_set.category}
    ids = [r.id for s in sets for r in s.rules]
    assert len(ids) == len(set(ids))


def test_rules_seed_platform_rows_only() -> None:
    for rule_set in _load():
        for rule in rule_set.rules:
            assert rule.tenant_id is None
            assert rule.rule_def.policy_ref is not None  # rules_platform_cited (DATABASE.md:504)


def test_rules_seed_banned_terms_listed() -> None:
    for rule_set in _load():
        banned = [r for r in rule_set.rules if isinstance(r.rule_def, BannedTerms)]
        assert len(banned) == 1
        assert banned[0].field == "content.title"
        definition = banned[0].rule_def
        assert isinstance(definition, BannedTerms)
        assert set(definition.terms) == BANNED
        by_type = {r.rule_def.type: r.rule_def for r in rule_set.rules}
        assert by_type["max_length"].limit == 200  # type: ignore[union-attr]
        assert by_type["min_length"].limit == 5  # type: ignore[union-attr]


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
