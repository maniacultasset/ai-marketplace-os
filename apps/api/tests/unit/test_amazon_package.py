"""Item 15 (client feedback 2026-11-11): the packed box goes to Amazon.

A sellable item (standalone, child) sends `item_package_dimensions` when all three sides are
given and `item_package_weight` when the weight is; a parent sends neither, and a missing package
is never an error. The seller facts carry the box from the resolved product facts, and the LISTING
fixtures accept the shapes."""

from __future__ import annotations

import dataclasses
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from app.adapters.amazon import constants as c
from app.adapters.amazon import mapping
from app.adapters.amazon.drift import diff, sellable_names
from app.adapters.base import PACKAGE_PATHS, SellerFacts, fact_paths
from app.adapters.flipkart import constants as flipkart
from app.core.resolved_facts import Package, ProductFacts
from app.services.requirements import needs_for
from app.services.seller_facts import seller_facts
from mkos_schemas.enums import Marketplace
from tests.adapters.amazon_support import (
    SELLER_FACTS,
    amazon_adapter,
    amazon_family,
    amazon_item,
    template,
)
from tests.support.fake_marketplace import build_fake, ptd_fixture
from tests.unit.test_amazon_schema_check import ready

MID = mapping.DEFAULT_MARKETPLACE_ID
BOX = dataclasses.replace(
    SELLER_FACTS,
    package_length_cm=Decimal("30.00"),
    package_breadth_cm=Decimal("22.50"),
    package_height_cm=Decimal("4.00"),
    package_weight_g=350,
)
DIMENSIONS = {
    "length": {"value": 30, "unit": "centimeters"},
    "width": {"value": 22.5, "unit": "centimeters"},
    "height": {"value": 4, "unit": "centimeters"},
    "marketplace_id": MID,
}
WEIGHT = {"value": 350, "unit": "grams", "marketplace_id": MID}


def _attrs(item: Any) -> dict[str, Any]:
    return item.payload["attributes"]  # type: ignore[no-any-return]


def _standalone(facts: SellerFacts) -> Any:
    sent = amazon_item()
    return dataclasses.replace(sent, unit_facts={sent.listing.product_id: facts})


def _one(facts: SellerFacts) -> Any:
    items, errors = mapping.build(_standalone(facts))
    assert errors == [], [e.model_dump(mode="json") for e in errors]
    (item,) = items
    return item


def test_all_sides_and_weight_are_sent() -> None:
    item = _one(BOX)
    assert _attrs(item)[c.PACKAGE_DIMENSIONS] == [DIMENSIONS]
    assert _attrs(item)[c.PACKAGE_WEIGHT] == [WEIGHT]
    assert {"facts.package_dimensions", "facts.package_weight"} <= set(item.submitted)
    assert item.payload["requirements"] == "LISTING"


def test_a_missing_side_sends_no_dimensions_but_the_weight() -> None:
    item = _one(dataclasses.replace(BOX, package_height_cm=None))
    assert c.PACKAGE_DIMENSIONS not in _attrs(item)
    assert _attrs(item)[c.PACKAGE_WEIGHT] == [WEIGHT]
    assert "facts.package_dimensions" not in item.submitted


def test_sides_without_weight_send_the_dimensions_only() -> None:
    item = _one(dataclasses.replace(BOX, package_weight_g=None))
    assert _attrs(item)[c.PACKAGE_DIMENSIONS] == [DIMENSIONS]
    assert c.PACKAGE_WEIGHT not in _attrs(item)
    assert "facts.package_weight" not in item.submitted


def test_no_package_sends_neither_and_is_no_error() -> None:
    item = _one(SELLER_FACTS)
    assert c.PACKAGE_DIMENSIONS not in _attrs(item)
    assert c.PACKAGE_WEIGHT not in _attrs(item)
    assert not {"facts.package_dimensions", "facts.package_weight"} & set(item.paths)


def test_a_zero_side_is_not_sent() -> None:
    item = _one(dataclasses.replace(BOX, package_length_cm=Decimal("0")))
    assert c.PACKAGE_DIMENSIONS not in _attrs(item)


def test_children_send_the_box_and_the_parent_never() -> None:
    sent = amazon_family()
    sent = dataclasses.replace(sent, unit_facts={u.product_id: BOX for u in sent.units()})
    items, errors = mapping.build(sent)
    assert errors == []
    parent, *children = items
    assert parent.unit.item_role == "parent"
    assert c.PACKAGE_DIMENSIONS not in _attrs(parent)
    assert c.PACKAGE_WEIGHT not in _attrs(parent)
    assert "facts.package_dimensions" not in parent.paths
    assert children
    for child in children:
        assert child.unit.item_role == "child"
        assert _attrs(child)[c.PACKAGE_DIMENSIONS] == [DIMENSIONS]
        assert _attrs(child)[c.PACKAGE_WEIGHT] == [WEIGHT]


def test_reverse_index_maps_package_issues_to_their_facts() -> None:
    index = mapping.reverse_index(template().template)
    assert index[c.PACKAGE_DIMENSIONS] == ("facts.package_dimensions",)
    assert index[c.PACKAGE_WEIGHT] == ("facts.package_weight",)


@pytest.mark.parametrize(("category", "product_type"), [("ethnic_wear_kurta_sets", "KURTA")])
async def test_the_box_passes_the_listing_fixture(category: str, product_type: str) -> None:
    schema: dict[str, Any] = json.loads(ptd_fixture(f"{product_type}.listing.schema.json"))
    sellable = await ready(schema, product_type=product_type)
    sent = amazon_item(template=template(category), product_type_schema=sellable)
    sent = dataclasses.replace(sent, unit_facts={sent.listing.product_id: BOX})
    items, errors = mapping.build(sent)
    ptd = [e for e in errors if (e.raw_code or "").startswith("ptd:")]
    assert ptd == [], [e.model_dump(mode="json") for e in ptd]
    assert c.PACKAGE_DIMENSIONS in _attrs(items[0])


async def test_a_wrong_unit_fails_the_listing_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    # guard-must-fail: the fixture's unit enum is checked, so a bad spelling would be caught
    schema: dict[str, Any] = json.loads(ptd_fixture("KURTA.listing.schema.json"))
    sellable = await ready(schema)
    sent = amazon_item(product_type_schema=sellable)
    sent = dataclasses.replace(sent, unit_facts={sent.listing.product_id: BOX})
    monkeypatch.setattr(c, "PACKAGE_WEIGHT_UNIT", "gram")
    _, errors = mapping.build(sent)
    assert any((e.raw_code or "").startswith("ptd:") for e in errors)


def test_package_names_are_sellable_and_never_drift_as_unfed() -> None:
    assert {c.PACKAGE_DIMENSIONS, c.PACKAGE_WEIGHT} <= set(sellable_names())
    schema = {"properties": {}, "required": [c.PACKAGE_DIMENSIONS, c.PACKAGE_WEIGHT]}
    assert [d for d in diff(schema, [], "KURTA") if d.kind == "unfed_required"] == []


def test_package_is_needed_on_amazon_and_flipkart_still_never_sends_it() -> None:
    for category in ("tshirts", "home_decor_lamps", "unknown_slug"):
        needed = set(needs_for(Marketplace.AMAZON, category))
        assert {"package_dimensions", "package_weight"} <= needed
    assert {"package_length", "package_weight"} <= flipkart.FORBIDDEN_ATTRIBUTES


def test_seller_facts_carry_the_box() -> None:
    facts = ProductFacts(
        product_id=uuid.uuid4(),
        sku="SKU-1",
        level="variant",
        package=Package(length_cm="30.00", breadth_cm="22.50", height_cm="4.00", weight_g=350),
    )
    out = seller_facts(facts)
    assert (out.package_length_cm, out.package_breadth_cm, out.package_height_cm) == (
        Decimal("30.00"),
        Decimal("22.50"),
        Decimal("4.00"),
    )
    assert out.package_weight_g == 350
    empty = seller_facts(ProductFacts(product_id=uuid.uuid4(), sku="S", level="standalone"))
    assert empty.package_length_cm is None and empty.package_weight_g is None


# ---- a live listing's update carries the box ----


def test_sellable_items_answer_for_the_box_and_a_parent_does_not() -> None:
    parent, child, *_rest = amazon_family().units()
    assert set(PACKAGE_PATHS) <= set(fact_paths(child))
    assert set(PACKAGE_PATHS) <= set(fact_paths(amazon_item().units()[0]))
    assert not set(PACKAGE_PATHS) & set(fact_paths(parent))


def _spy(adapter: Any) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    original = adapter._client.patch

    async def spy(sku: str, body: dict[str, Any]) -> Any:
        seen.append((sku, body))
        return await original(sku, body)

    adapter._client.patch = spy
    return seen


def _patched(body: dict[str, Any]) -> set[str]:
    return {p["path"] for p in body["patches"] if p["op"] == "replace"}


BOX_PATCHES = {
    f"/attributes/{c.PACKAGE_DIMENSIONS}",
    f"/attributes/{c.PACKAGE_WEIGHT}",
}


async def test_a_live_standalone_update_replaces_the_box() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _standalone(BOX)
    await adapter.create_listing(sent)
    seen = _spy(adapter)
    sku = sent.units()[0].seller_sku
    result = await adapter.update_listing(sent, fields={sku: sorted(fact_paths(sent.units()[0]))})
    (one,) = seen
    assert _patched(one[1]) >= BOX_PATCHES
    statuses = {f.field: f.status for f in result.fields}
    assert statuses["facts.package_dimensions"] == statuses["facts.package_weight"] == "succeeded"


async def test_a_live_family_update_sends_the_box_on_children_never_the_parent() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    family = amazon_family()
    boxed = dataclasses.replace(family, unit_facts={u.product_id: BOX for u in family.units()})
    await adapter.create_listing(boxed)
    seen = _spy(adapter)
    await adapter.update_listing(
        boxed, fields={u.seller_sku: sorted(fact_paths(u)) for u in boxed.units()}
    )
    parent, *children = boxed.units()
    bodies = dict(seen)
    assert not BOX_PATCHES & _patched(bodies.get(parent.seller_sku, {"patches": []}))
    assert children and all(_patched(bodies[u.seller_sku]) >= BOX_PATCHES for u in children)


async def test_a_retry_of_a_cleared_weight_never_raises() -> None:
    """A marketplace issue on the weight maps to `facts.package_weight`; once the seller clears
    the weight, the field retry must not refuse the path (the item still answers for it)."""
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _standalone(dataclasses.replace(BOX, package_weight_g=None))
    await adapter.create_listing(sent)
    seen = _spy(adapter)
    sku = sent.units()[0].seller_sku
    result = await adapter.update_listing(sent, fields={sku: ["facts.package_weight"]})
    assert seen == []
    assert {f.field for f in result.fields} == {"facts.package_weight"}
    assert all(f.status != "succeeded" for f in result.fields)
