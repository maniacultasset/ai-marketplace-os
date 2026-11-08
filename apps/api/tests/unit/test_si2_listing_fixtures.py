"""SI2 tester (AC13, PLAN §3.13): the real hand-written `LISTING` fixtures. With
`KURTA.listing.schema.json` / `SHIRT.listing.schema.json` stored for sellable items and the
PRODUCT_ONLY fixture for the parent, a priced, compliant standalone item and every child of a
family pass the stored-definition check; an unpriced child is flagged and the parent never is."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from app.adapters.amazon import mapping
from mkos_schemas.codes import ErrorCode
from tests.adapters.amazon_support import amazon_family, amazon_item, template
from tests.support.fake_marketplace import ptd_fixture
from tests.unit.test_amazon_schema_check import ready

CASES = [("ethnic_wear_kurta_sets", "KURTA"), ("tshirts", "SHIRT")]


def _schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(ptd_fixture(name))
    return loaded


def _ptd(errors: list[Any]) -> list[Any]:
    return [e for e in errors if (e.raw_code or "").startswith("ptd:")]


@pytest.mark.parametrize(("category", "product_type"), CASES)
async def test_priced_standalone_passes_the_real_listing_fixture(
    category: str, product_type: str
) -> None:
    sellable = await ready(
        _schema(f"{product_type}.listing.schema.json"), product_type=product_type
    )
    assert sellable.product_type == product_type
    sent = amazon_item(template=template(category), product_type_schema=sellable)
    # no product weight: the hand-written SHIRT fixtures' `item_weight` refuses the `unit` key
    # the adapter sends (pre-existing on main, fixture-only; DL1 replaces the fixtures)
    items, errors = mapping.build(sent)
    assert _ptd(errors) == [], [e.model_dump(mode="json") for e in _ptd(errors)]
    (only,) = items
    assert only.payload["requirements"] == "LISTING"
    assert "purchasable_offer" in only.payload["attributes"]


async def test_family_children_pass_and_unpriced_child_flagged_parent_never() -> None:
    sellable = await ready(_schema("KURTA.listing.schema.json"))
    parent_only = await ready(_schema("KURTA.schema.json"))
    family = amazon_family(product_type_schema=sellable, parent_type_schema=parent_only)
    items, errors = mapping.build(family)
    assert _ptd(errors) == [], [e.model_dump(mode="json") for e in _ptd(errors)]
    parent, first, *_ = family.units()
    assert items[0].payload["requirements"] == "LISTING_PRODUCT_ONLY"
    assert "purchasable_offer" not in items[0].payload["attributes"]

    facts = dict(family.unit_facts)
    facts[first.product_id] = dataclasses.replace(
        facts[first.product_id], mrp=None, selling_price=None
    )
    unpriced = dataclasses.replace(family, unit_facts=facts)
    _, errors = mapping.build(unpriced)
    fields = {e.field for e in errors if e.error_code is ErrorCode.MISSING_ATTRIBUTE}
    assert f"variants[{first.seller_sku}].facts.mrp" in fields
    assert f"variants[{first.seller_sku}].facts.selling_price" in fields
    flagged = {e.field for e in _ptd(errors)}
    assert not any(parent.seller_sku in f and first.seller_sku not in f for f in flagged)
