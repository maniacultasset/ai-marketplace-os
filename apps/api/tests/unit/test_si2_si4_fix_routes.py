"""SI2 x SI4: the fix policy reads the errors SI2's adapters and overlay produce. Price and
manufacturer errors at `facts.*` (and `variants[<sku>].facts.*`) become one seller question, a
marketplace issue on the manufacturer stays a review with no address, and a seller value that
fails its value map is never remapped by code."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from app.adapters.amazon.mapping import _offer
from app.adapters.base import PARTY_REJECTED_MESSAGE, AdapterItem, SellerFacts, issue_text
from app.adapters.error_map import ErrorMapper
from app.services.auto_fix.fixers import FixContext, fix_remap_value
from app.services.auto_fix.policy import (
    MARKETPLACE_ERROR_REVIEW,
    MISSING_FACT,
    Route,
    ask_kind,
    fact_key,
    route,
)
from app.services.seller_facts import seller_overlay
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace
from mkos_schemas.errors import ErrorItem
from mkos_schemas.rules import RuleSet
from tests.unit.test_seller_overlay import facts, kurta_template, listing

NOW = "2026-11-07T00:00:00Z"
STANDALONE = AdapterItem(uuid.UUID(int=1), "KURTA-1", "standalone")
CHILD = AdapterItem(uuid.UUID(int=2), "KURTA-1-M", "child", {"size": "M"})
PRICED = SellerFacts(mrp=Decimal("999.00"), selling_price=Decimal("499.00"), currency="INR")


def _routed(item: ErrorItem, source: str = "amazon_adapter") -> tuple[Route, str | None]:
    r = route(item, source=source, critical_keys=frozenset())
    return r.route, ask_kind(r)


@pytest.mark.parametrize(
    ("unit", "facts_", "code", "path"),
    [
        (STANDALONE, SellerFacts(), ErrorCode.MISSING_ATTRIBUTE, "facts.mrp"),
        (CHILD, SellerFacts(mrp=Decimal("999.00")), ErrorCode.MISSING_ATTRIBUTE, "selling_price"),
        (
            STANDALONE,
            SellerFacts(mrp=Decimal("100.00"), selling_price=Decimal("499.00")),
            ErrorCode.PRICE_ABOVE_MRP,
            "facts.selling_price",
        ),
        (
            STANDALONE,
            SellerFacts(mrp=Decimal("999.00"), selling_price=Decimal("499.00"), currency="USD"),
            ErrorCode.VALUE_NOT_ALLOWED,
            "facts.currency",
        ),
    ],
)
def test_price_errors_become_one_seller_question(
    unit: AdapterItem, facts_: SellerFacts, code: ErrorCode, path: str
) -> None:
    offer, errors = _offer(unit, facts_, "A21TJRUUN4KGV")
    assert offer is None
    (item,) = [e for e in errors if e.error_code is code and e.field.endswith(path)]
    assert item.field.endswith(path)
    if unit.item_role == "child":
        assert item.field.startswith("variants[KURTA-1-M].facts.")
    assert fact_key(item.field) == item.field.rsplit(".", 1)[1]
    assert _routed(item) in {(Route.ASK, MISSING_FACT), (Route.DETERMINISTIC, MISSING_FACT)}


def test_a_price_error_on_a_critical_key_is_still_a_question() -> None:
    _offer_none, errors = _offer(CHILD, SellerFacts(), "A21TJRUUN4KGV")
    for item in errors:
        r = route(item, source="amazon_adapter", critical_keys=frozenset({"mrp", "selling_price"}))
        assert (r.route, ask_kind(r)) == (Route.ASK, MISSING_FACT)


def test_a_facts_value_error_is_never_remapped_by_code() -> None:
    """VALUE_NOT_ALLOWED routes to REMAP_VALUE with a question fallback; at `facts.*` the remap
    fixer never applies (the value is the seller's fact, not a listing slot)."""
    usd = SellerFacts(mrp=PRICED.mrp, selling_price=PRICED.selling_price, currency="USD")
    _none, errors = _offer(STANDALONE, usd, "A21TJRUUN4KGV")
    (item,) = errors
    ctx = FixContext(ruleset=_ruleset(), template=kurta_template(), critical_keys=frozenset())
    assert fix_remap_value(listing(), item, ctx) is None
    assert ask_kind(route(item, source="amazon_adapter", critical_keys=frozenset())) == MISSING_FACT


def test_a_seller_colour_that_fails_its_value_map_is_never_remapped() -> None:
    template = kurta_template()
    body = seller_overlay(listing(color="navy blue"), facts({"color": "neon-plaid"}), template)
    assert body.attributes["color"] == "neon-plaid" and "color" in body.attribute_sources
    item = ErrorItem.model_validate(
        {
            "field": "attributes.color",
            "error_code": ErrorCode.VALUE_NOT_ALLOWED,
            "severity": "error",
            "message": "m",
            "source": "rules_engine",
            "retryable": False,
        }
    )
    ctx = FixContext(ruleset=_ruleset(), template=template, critical_keys=frozenset())
    assert fix_remap_value(body, item, ctx) is None
    assert ask_kind(route(item, source="rules_engine", critical_keys=frozenset())) == MISSING_FACT


def test_a_marketplace_manufacturer_issue_is_a_review_without_the_address() -> None:
    # what `party_secrets` lists for this party: the name, the address and its runs
    parties = (
        "Test Mills Pvt Ltd, 1 Mill Road, Surat",
        "1 Mill Road, Surat",
        "Test Mills Pvt Ltd",
        "1 Mill Road",
        "Surat",
    )
    index = {"manufacturer_contact_information": ("facts.manufacturer",)}
    text = issue_text(
        "manufacturer_contact_information: TEST MILLS PVT LTD / 1 mill road",
        ["manufacturer_contact_information"],
        index,
        parties,
    )
    item = ErrorMapper([]).to_item(
        raw_code="90220",
        raw_message=text,
        field="facts.manufacturer",
        marketplace=Marketplace.AMAZON,
    )
    assert item.message == PARTY_REJECTED_MESSAGE
    assert fact_key(item.field) == "manufacturer"
    assert _routed(item) == (Route.ASK, MARKETPLACE_ERROR_REVIEW)
    loose = issue_text("Bad value near test mills pvt ltd; 1 MILL ROAD", ["item_name"], {}, parties)
    assert "mill" not in loose.casefold()


def _ruleset() -> RuleSet:
    return RuleSet(marketplace="amazon", category="ethnic_wear_kurta_sets", loaded_at=NOW)
