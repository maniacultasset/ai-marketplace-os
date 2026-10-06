"""S14a T12: the seed RuleSets resolve and run over the seed products (AC 10; D5, D12, R11577).

Each seed RuleSet's rows go through `resolve_ruleset` (as S14b's loader will) and then `evaluate`.
The goldens pin `attempts_used=0` (S18 sets it) and the `*` rule's suggestion (CODE-PLAN L5); the
duplicate BANNED_TERM items from the title rule and the `*` rule are accepted (L4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.services.rules_engine import RuleRow, evaluate, resolve_ruleset
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.rules import RuleSet
from pydantic import TypeAdapter
from tests.rules_engine_builders import FIXTURES, LOADED_AT

HERE = Path(__file__).parent
LISTINGS = HERE / "fixtures"
GOLDEN = HERE / "golden"
SEED = FIXTURES / "rules.seed.json"
PRODUCTS = ("kurta_001", "kurta_002", "decor_001", "fsh_tee_001")
MARKETPLACES = ("amazon", "flipkart")
CATEGORY_PRODUCT = {
    "ethnic_wear_kurta_sets": "kurta_002",
    "home_decor_lamps": "decor_001",
    "tshirts": "fsh_tee_001",
}


def _seed() -> dict[tuple[str, str], RuleSet]:
    sets = TypeAdapter(list[RuleSet]).validate_json(SEED.read_bytes())
    return {(s.marketplace.value, s.category): s for s in sets}


def _listing(product: str, marketplace: str, **over: Any) -> CanonicalListing:
    data = json.loads((LISTINGS / f"{product}.{marketplace}.json").read_text(encoding="utf-8"))
    data.update(over)
    return CanonicalListing.model_validate(data)


def _resolved(marketplace: str, category: str) -> RuleSet:
    seed = _seed()[(marketplace, category)]
    rows = [RuleRow(rule, revision=index + 1) for index, rule in enumerate(seed.rules)]
    return resolve_ruleset(
        rows, marketplace=seed.marketplace, category=category, tenant_id=None, loaded_at=LOADED_AT
    )


def _run(item: CanonicalListing) -> Any:
    return evaluate(item, _resolved(item.marketplace.value, item.category))


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize("product", PRODUCTS)
def test_rules_seed_resolves_and_runs(product: str, marketplace: str) -> None:
    item = _listing(product, marketplace)
    assert item.marketplace.value == marketplace
    rep = _run(item)
    assert rep.listing_id == item.listing_id
    assert rep.status in ("passed", "failed")


def test_rules_seed_every_ruleset_has_a_fixture_listing() -> None:
    covered = {
        (_listing(p, m).marketplace.value, _listing(p, m).category)
        for p in PRODUCTS
        for m in MARKETPLACES
    }
    assert covered == set(_seed())


@pytest.mark.parametrize("category", list(CATEGORY_PRODUCT))
def test_rules_seed_flipkart_resolves_to_150(category: str) -> None:
    resolved = _resolved("flipkart", category)
    (title_max,) = [r for r in resolved.rules if r.rule_def.type == "max_length"]
    assert title_max.rule_def.limit == 150  # type: ignore[union-attr]
    item = _listing(CATEGORY_PRODUCT[category], "flipkart")
    long_title = item.model_copy(
        update={"content": item.content.model_copy(update={"title": "x" * 151})}
    )
    codes = [(e.field, e.error_code.value) for e in _run(long_title).errors]
    assert ("content.title", "FIELD_TOO_LONG") in codes
    amazon = _resolved("amazon", category)
    (amazon_max,) = [r for r in amazon.rules if r.rule_def.type == "max_length"]
    assert amazon_max.rule_def.limit == 200  # type: ignore[union-attr]


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize("term", ["best", "guaranteed", "Best", "GUARANTEED"])
def test_rules_seed_bare_best_flagged(marketplace: str, term: str) -> None:
    """R11577/R11577a: a bare "best" or "guaranteed" in a title fails on every seeded RuleSet."""
    for product in PRODUCTS:
        item = _listing(product, marketplace)
        title = f"{term} {item.content.title}"
        item = item.model_copy(update={"content": item.content.model_copy(update={"title": title})})
        rep = _run(item)
        assert rep.status == "failed"
        hits = [
            e
            for e in rep.errors
            if e.field == "content.title" and e.error_code.value == "BANNED_TERM"
        ]
        assert hits, (product, marketplace)
        assert any(term.casefold() in e.message.casefold() for e in hits)


@pytest.mark.parametrize("marketplace", MARKETPLACES)
def test_rules_seed_golden_kurta(marketplace: str) -> None:
    rep = _run(_listing("kurta_001", marketplace))
    golden = json.loads((GOLDEN / f"seed_kurta_001.{marketplace}.json").read_text(encoding="utf-8"))
    assert rep.model_dump(mode="json") == golden
    assert golden["status"] == "failed" and golden["attempts_used"] == 0
    assert any(e["suggestion"] for e in golden["errors"])


def test_rules_seed_golden_fsh_tee_001_amazon() -> None:
    rep = _run(_listing("fsh_tee_001", "amazon"))
    golden = json.loads((GOLDEN / "seed_fsh_tee_001.amazon.json").read_text(encoding="utf-8"))
    assert rep.model_dump(mode="json") == golden
    assert golden["status"] == "passed" and golden["errors"] == []


@pytest.mark.parametrize("marketplace", MARKETPLACES)
def test_rules_seed_tshirts_missing_fabric_type_fails(marketplace: str) -> None:
    item = _listing("fsh_tee_001", marketplace)
    attributes = {k: v for k, v in item.attributes.items() if k != "fabric_type"}
    rep = _run(item.model_copy(update={"attributes": attributes}))
    assert rep.status == "failed"
    assert [(e.field, e.error_code.value) for e in rep.errors] == [
        ("attributes.fabric_type", "MISSING_ATTRIBUTE")
    ]
