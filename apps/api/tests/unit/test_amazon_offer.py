"""SI2 T7 (AC1, AC2, AC3; D3, D4): the Amazon offer on sellable items."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest
from app.adapters.amazon import mapping
from app.adapters.base import SellerFacts
from tests.adapters.amazon_support import (
    SELLER_FACTS,
    amazon_adapter,
    amazon_family,
    amazon_item,
    template,
)
from tests.support.fake_marketplace import build_fake


def _facts(**over: Any) -> SellerFacts:
    return dataclasses.replace(SELLER_FACTS, **over)


def _attrs(item: Any) -> dict[str, Any]:
    return item.payload["attributes"]  # type: ignore[no-any-return]


def _codes(errors: list[Any]) -> set[tuple[str, str]]:
    return {(e.field, e.error_code.value) for e in errors}


def _standalone(**facts: Any) -> Any:
    sent = amazon_item()
    return dataclasses.replace(sent, unit_facts={sent.listing.product_id: _facts(**facts)})


def test_standalone_offer_exact_values() -> None:
    items, errors = mapping.build(_standalone())
    assert errors == []
    (item,) = items
    assert item.payload["requirements"] == "LISTING"
    attrs = _attrs(item)
    (offer,) = attrs["purchasable_offer"]
    assert offer["currency"] == "INR" and offer["marketplace_id"] == mapping.DEFAULT_MARKETPLACE_ID
    our = offer["our_price"][0]["schedule"][0]["value_with_tax"]
    mrp = offer["maximum_retail_price"][0]["schedule"][0]["value_with_tax"]
    assert (our, mrp) == (499.0, 999.0)
    assert Decimal(repr(our)) == Decimal("499.00") and Decimal(repr(mrp)) == Decimal("999.00")
    assert attrs["condition_type"] == [
        {"value": "new_new", "marketplace_id": mapping.DEFAULT_MARKETPLACE_ID}
    ]
    assert {"facts.mrp", "facts.selling_price"} <= set(item.submitted)


def test_precision_kept() -> None:
    (item,) = mapping.build(_standalone(selling_price=Decimal("1.10"), mrp=Decimal("1.10")))[0]
    value = _attrs(item)["purchasable_offer"][0]["our_price"][0]["schedule"][0]["value_with_tax"]
    assert value == 1.1 and Decimal(repr(value)).quantize(Decimal("0.01")) == Decimal("1.10")


def test_family_parent_has_no_offer_children_their_own() -> None:
    sent = amazon_family()
    parent, *kids = sent.units()
    prices = {k.product_id: _facts(selling_price=Decimal(400 + n)) for n, k in enumerate(kids)}
    sent = dataclasses.replace(sent, unit_facts={parent.product_id: SELLER_FACTS, **prices})
    items, errors = mapping.build(sent)
    assert errors == []
    by_role = {i.unit.seller_sku: i for i in items}
    top = by_role[parent.seller_sku]
    assert top.payload["requirements"] == "LISTING_PRODUCT_ONLY"
    assert "purchasable_offer" not in _attrs(top) and "condition_type" not in _attrs(top)
    assert "facts.mrp" not in top.paths
    seen = set()
    for kid in kids:
        one = by_role[kid.seller_sku]
        assert one.payload["requirements"] == "LISTING"
        price = _attrs(one)["purchasable_offer"][0]["our_price"][0]["schedule"][0]
        seen.add(price["value_with_tax"])
    assert len(seen) == len(kids)


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"mrp": None}, {("facts.mrp", "MISSING_ATTRIBUTE")}),
        ({"selling_price": None}, {("facts.selling_price", "MISSING_ATTRIBUTE")}),
        (
            {"selling_price": Decimal("1000.00"), "mrp": Decimal("999.00")},
            {("facts.selling_price", "PRICE_ABOVE_MRP")},
        ),
        ({"currency": "USD"}, {("facts.currency", "VALUE_NOT_ALLOWED")}),
        ({"selling_price": Decimal("0.00")}, {("facts.selling_price", "VALUE_NOT_ALLOWED")}),
        ({"mrp": Decimal("-1.00")}, {("facts.mrp", "VALUE_NOT_ALLOWED")}),
        ({"mrp": Decimal("10000000000.00")}, {("facts.mrp", "VALUE_NOT_ALLOWED")}),
    ],
)
def test_offer_errors_block(facts: dict[str, Any], expected: set[tuple[str, str]]) -> None:
    sent = _standalone(**facts)
    items, errors = mapping.build(sent)
    assert _codes(errors) == expected
    assert "purchasable_offer" not in _attrs(items[0])
    assert set(_codes(mapping.check(sent))) == expected
    assert mapping.preview(sent).report.status == "failed"


def test_offer_messages_carry_no_party() -> None:
    errors = mapping.check(_standalone(mrp=None, currency="USD"))
    for e in errors:
        assert "Mill" not in e.message and "Mill" not in (e.value_submitted or "")


@pytest.mark.parametrize("ok", [{"selling_price": Decimal("999.00")}, {"currency": " inr "}])
def test_offer_edges_pass(ok: dict[str, Any]) -> None:
    assert mapping.check(_standalone(**ok)) == []


def test_child_errors_at_variant_paths() -> None:
    sent = amazon_family()
    parent, first, *rest = sent.units()
    facts = {u.product_id: SELLER_FACTS for u in (parent, *rest)}
    facts[first.product_id] = _facts(mrp=None)
    errors = mapping.check(dataclasses.replace(sent, unit_facts=facts))
    assert _codes(errors) == {(f"variants[{first.seller_sku}].facts.mrp", "MISSING_ATTRIBUTE")}


def test_no_unit_facts_fails_closed() -> None:
    sent = dataclasses.replace(amazon_item(), unit_facts={})
    codes = _codes(mapping.check(sent))
    assert ("facts.mrp", "MISSING_ATTRIBUTE") in codes
    assert ("facts.selling_price", "MISSING_ATTRIBUTE") in codes


def test_lamp_gets_the_offer() -> None:
    from tests.unit.test_seed_templates import _decor_001  # the seeded lamp listing

    sent = amazon_item(_decor_001("amazon"), template=template("home_decor_lamps"))
    (item,) = mapping.build(sent)[0]
    assert "purchasable_offer" in _attrs(item)


async def test_create_with_a_price_error_sends_nothing() -> None:
    app = build_fake()
    result = await amazon_adapter(app).create_listing(_standalone(mrp=None))
    assert result.status == "failed"
    assert app.state.fake.calls == []
