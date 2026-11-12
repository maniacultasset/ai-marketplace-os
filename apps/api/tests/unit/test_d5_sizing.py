"""Client items 8 and 15 (12 Nov): the size and the product's own measurements in the Amazon
payload, each only where the stored LISTING definition declares it and in the shape it accepts.

- item 8 bullet 5: the structured `apparel_size` (India size system, alpha or numeric class, an
  allowed size) and the size-chart row as `{value, unit}` measurements;
- item 15 bullet 1: the product itself (`item_dimensions`, `item_weight`) apart from the packed
  box (`item_package_*`), and the `L x B x H cm` text it is stored as."""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from app.adapters.amazon import constants as c
from app.adapters.amazon import mapping, sizing
from app.adapters.base import SellerFacts, item_dimensions_text, parse_item_dimensions
from app.core.resolved_facts import ProductFacts, ResolvedFact
from app.services.seller_facts import seller_facts
from tests.adapters.amazon_support import SELLER_FACTS, amazon_family, amazon_item, template
from tests.support.fake_marketplace import ptd_fixture
from tests.unit.test_amazon_schema_check import ready

MID = mapping.DEFAULT_MARKETPLACE_ID
TIMES = chr(0xD7)  # the multiplication sign
MEASURE: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["value", "unit"],
        "properties": {
            "value": {"type": "number"},
            "unit": {"type": "string", "enum": ["centimeters", "inches"]},
            "marketplace_id": {"type": "string"},
        },
        "additionalProperties": False,
    },
}
SIDE: dict[str, Any] = {
    "type": "object",
    "required": ["value", "unit"],
    "properties": {
        "value": {"type": "number"},
        "unit": {"type": "string", "enum": ["centimeters", "inches"]},
    },
}
APPAREL_SIZE: dict[str, Any] = {
    "type": "array",
    "maxItems": 1,
    "items": {
        "type": "object",
        "required": ["size_system", "size_class", "size", "marketplace_id"],
        "properties": {
            "size_system": {"type": "string", "enum": ["as1", "as6"], "enumNames": ["US", "IN"]},
            "size_class": {
                "type": "string",
                "enum": ["alpha", "numeric"],
                "enumNames": ["Alpha", "Numeric"],
            },
            "size": {
                "type": "string",
                "enum": ["s", "m", "l", "xxl", "38", "40"],
                "enumNames": ["S", "M", "L", "XXL", "38", "40"],
            },
            "body_type": {"type": "string", "enum": ["regular", "plus"]},
            "marketplace_id": {"type": "string"},
        },
        "additionalProperties": False,
    },
}
ITEM_DIMENSIONS: dict[str, Any] = {
    "type": "array",
    "maxItems": 1,
    "items": {
        "type": "object",
        "required": ["length", "width", "height"],
        "properties": {
            "length": SIDE,
            "width": SIDE,
            "height": SIDE,
            "marketplace_id": {"type": "string"},
        },
        "additionalProperties": False,
    },
}
WEIGHT: dict[str, Any] = copy.deepcopy(MEASURE)
WEIGHT["items"]["properties"]["unit"]["enum"] = ["grams", "kilograms"]
SIZED = dataclasses.replace(
    SELLER_FACTS,
    size="XXL",
    size_chart_unit="cm",
    size_chart_basis="garment",
    size_measures={"chest": Decimal("112"), "length": Decimal("108.5"), "hip": Decimal("120")},
    item_length_cm=Decimal("30"),
    item_breadth_cm=Decimal("15"),
    item_height_cm=Decimal("12.5"),
    product_weight_g=420,
)


def _kurta(**extra: Any) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(ptd_fixture("KURTA.listing.schema.json"))
    schema["properties"].update(extra)
    return schema


def _attrs(item: Any) -> dict[str, Any]:
    return item.payload["attributes"]  # type: ignore[no-any-return]


async def _standalone(facts: SellerFacts, schema: dict[str, Any] | None, **over: Any) -> Any:
    sellable = None if schema is None else await ready(schema)
    sent = amazon_item(product_type_schema=sellable, **over)
    sent = dataclasses.replace(sent, unit_facts={sent.listing.product_id: facts})
    items, errors = mapping.build(sent)
    ptd = [e for e in errors if (e.raw_code or "").startswith("ptd:")]
    assert ptd == [], [e.model_dump(mode="json") for e in ptd]
    return items[0]


# ---- the stored text ----


def test_item_dimensions_text_round_trips() -> None:
    text = item_dimensions_text(Decimal("30.00"), Decimal("15"), Decimal("12.50"))
    assert text == "30 x 15 x 12.5 cm"
    assert parse_item_dimensions(text) == (Decimal(30), Decimal(15), Decimal("12.5"))
    assert parse_item_dimensions("30X15 x 12.5CM") == (Decimal(30), Decimal(15), Decimal("12.5"))
    assert parse_item_dimensions(f"30 {TIMES} 15 {TIMES} 12 cm") == (
        Decimal(30),
        Decimal(15),
        Decimal(12),
    )


@pytest.mark.parametrize(
    "text", ["30 x 15 cm", "about 30 cm long", "30 x 15 x 12 in", "0 x 15 x 12 cm", None, 30]
)
def test_other_text_is_not_a_product_size(text: Any) -> None:
    assert parse_item_dimensions(text) is None


# ---- item 8: apparel size and the chart ----


async def test_apparel_size_is_sent_in_the_declared_shape() -> None:
    item = await _standalone(SIZED, _kurta(apparel_size=APPAREL_SIZE))
    assert _attrs(item)[sizing.APPAREL_SIZE] == [
        {
            "size_system": "as6",
            "size_class": "alpha",
            "size": "xxl",
            "body_type": "regular",
            "marketplace_id": MID,
        }
    ]
    assert "attributes.size" in item.submitted


async def test_numeric_size_and_alias() -> None:
    entry = sizing.apparel_size(
        dataclasses.replace(SIZED, size="40"), await ready(_kurta(apparel_size=APPAREL_SIZE)), MID
    )
    assert entry is not None and (entry["size_class"], entry["size"]) == ("numeric", "40")
    alias = sizing.apparel_size(
        dataclasses.replace(SIZED, size="2XL"), await ready(_kurta(apparel_size=APPAREL_SIZE)), MID
    )
    assert alias is not None and alias["size"] == "xxl"


async def test_a_size_the_definition_does_not_list_sends_no_apparel_size() -> None:
    schema = await ready(_kurta(apparel_size=APPAREL_SIZE))
    assert sizing.apparel_size(dataclasses.replace(SIZED, size="XS"), schema, MID) is None
    assert sizing.apparel_size(dataclasses.replace(SIZED, size="Kids 4-5"), schema, MID) is None


async def test_no_india_size_system_sends_no_apparel_size() -> None:
    shape = copy.deepcopy(APPAREL_SIZE)
    shape["items"]["properties"]["size_system"] = {
        "type": "string",
        "enum": ["as1", "as2"],
        "enumNames": ["US", "UK"],
    }
    assert sizing.apparel_size(SIZED, await ready(_kurta(apparel_size=shape)), MID) is None


async def test_no_stored_definition_or_no_declared_attribute_sends_nothing() -> None:
    item = await _standalone(SIZED, None)
    assert sizing.APPAREL_SIZE not in _attrs(item)
    assert "chest_size" not in _attrs(item)
    fixture = await _standalone(SIZED, _kurta())
    assert sizing.APPAREL_SIZE not in _attrs(fixture)
    assert sizing.ITEM_DIMENSIONS not in _attrs(fixture)
    assert sizing.ITEM_WEIGHT not in _attrs(fixture)


async def test_chart_row_goes_only_where_declared_and_in_its_unit() -> None:
    item = await _standalone(SIZED, _kurta(chest_size=MEASURE, item_length=MEASURE))
    assert _attrs(item)["chest_size"] == [
        {"value": 112, "unit": "centimeters", "marketplace_id": MID}
    ]
    assert _attrs(item)["item_length"] == [
        {"value": 108.5, "unit": "centimeters", "marketplace_id": MID}
    ]
    assert "hip_size" not in _attrs(item)  # the chart has it, the definition does not
    assert "facts.size_measurements" in item.submitted
    inches = await ready(_kurta(chest_size=WEIGHT))  # a unit list without lengths
    assert sizing.chart_measures(SIZED, inches, MID) == {}
    in_chart = dataclasses.replace(SIZED, size_chart_unit="in")
    found = sizing.chart_measures(in_chart, await ready(_kurta(chest_size=MEASURE)), MID)
    assert found["chest_size"][0]["unit"] == "inches"


async def test_children_send_their_own_size_and_the_parent_none() -> None:
    sellable = await ready(_kurta(apparel_size=APPAREL_SIZE, chest_size=MEASURE))
    sent = amazon_family(product_type_schema=sellable)
    facts = {
        u.product_id: dataclasses.replace(SIZED, size="M" if i % 2 else "L")
        for i, u in enumerate(sent.units())
    }
    items, _errors = mapping.build(dataclasses.replace(sent, unit_facts=facts))
    parent, *children = items
    assert sizing.APPAREL_SIZE not in _attrs(parent)
    assert "chest_size" not in _attrs(parent)
    assert {_attrs(child)[sizing.APPAREL_SIZE][0]["size"] for child in children} <= {"m", "l"}


# ---- item 15: the product itself, apart from the packed box ----


async def test_product_size_and_weight_go_apart_from_the_box() -> None:
    boxed = dataclasses.replace(
        SIZED,
        package_length_cm=Decimal(35),
        package_breadth_cm=Decimal(25),
        package_height_cm=Decimal(5),
        package_weight_g=600,
    )
    item = await _standalone(boxed, _kurta(item_dimensions=ITEM_DIMENSIONS, item_weight=WEIGHT))
    assert _attrs(item)[sizing.ITEM_DIMENSIONS] == [
        {
            "length": {"value": 30, "unit": "centimeters"},
            "width": {"value": 15, "unit": "centimeters"},
            "height": {"value": 12.5, "unit": "centimeters"},
            "marketplace_id": MID,
        }
    ]
    assert _attrs(item)[sizing.ITEM_WEIGHT] == [
        {"value": 420, "unit": "grams", "marketplace_id": MID}
    ]
    assert _attrs(item)[c.PACKAGE_DIMENSIONS][0]["length"]["value"] == 35
    assert _attrs(item)[c.PACKAGE_WEIGHT][0]["value"] == 600
    assert {"facts.product_dimensions", "facts.product_weight"} <= set(item.submitted)


async def test_lamp_sends_structured_item_dimensions_not_the_text() -> None:
    lamp = template("home_decor_lamps")
    body = amazon_item().listing.model_copy(
        update={"attributes": {"color": "Black", "product_dimensions": "30 x 15 x 12.5 cm"}}
    )
    schema: dict[str, Any] = json.loads(ptd_fixture("LAMP.schema.json"))
    item = await _standalone(SIZED, schema, body=body, template=lamp)
    (entry,) = _attrs(item)[sizing.ITEM_DIMENSIONS]
    assert entry["width"] == {"value": 15, "unit": "centimeters"}
    assert "attributes.product_dimensions" in item.submitted
    # no stored definition: the template's own mapping still decides, now structured
    bare = await _standalone(SIZED, None, body=body, template=lamp)
    assert _attrs(bare)[sizing.ITEM_DIMENSIONS][0]["length"]["value"] == 30


async def test_free_text_product_size_keeps_the_template_text() -> None:
    lamp = template("home_decor_lamps")
    body = amazon_item().listing.model_copy(
        update={"attributes": {"color": "Black", "product_dimensions": "about a foot tall"}}
    )
    loose = dataclasses.replace(SIZED, item_length_cm=None, item_breadth_cm=None)
    sent = amazon_item(body=body, template=lamp)
    sent = dataclasses.replace(sent, unit_facts={sent.listing.product_id: loose})
    items, _errors = mapping.build(sent)
    assert _attrs(items[0])[sizing.ITEM_DIMENSIONS][0]["value"] == "about a foot tall"


async def test_a_definition_unit_list_without_cm_sends_no_item_dimensions() -> None:
    shape = copy.deepcopy(ITEM_DIMENSIONS)
    for side in ("length", "width", "height"):
        shape["items"]["properties"][side] = {
            "type": "object",
            "properties": {"value": {"type": "number"}, "unit": {"enum": ["inches"]}},
        }
    found = sizing.item_size(SIZED, await ready(_kurta(item_dimensions=shape)), MID)
    assert sizing.ITEM_DIMENSIONS not in found


# ---- the seller facts ----


def _fact(key: str, **over: Any) -> ResolvedFact:
    return ResolvedFact(key=key, source="seller", **over)


def test_seller_facts_carry_the_size_row_and_the_product_size() -> None:
    chart = {
        "unit": "cm",
        "basis": "garment",
        "rows": [{"size": "M", "chest": "100"}, {"size": "X L", "chest": "112", "hip": "120.5"}],
    }
    facts = ProductFacts(
        product_id=uuid.uuid4(),
        sku="K-XL",
        level="variant",
        size_key="xl",
        product_weight_g=420,
        attributes={
            "size_measurements": _fact("size_measurements", normalized=chart),
            "product_dimensions": _fact("product_dimensions", value="30 x 15 x 12.5 cm"),
        },
    )
    out = seller_facts(facts)
    assert out.size == "xl"
    assert (out.size_chart_unit, out.size_chart_basis) == ("cm", "garment")
    assert dict(out.size_measures) == {"chest": Decimal(112), "hip": Decimal("120.5")}
    assert (out.item_length_cm, out.item_breadth_cm, out.item_height_cm) == (
        Decimal(30),
        Decimal(15),
        Decimal("12.5"),
    )
    assert out.product_weight_g == 420


def test_a_style_or_an_unmatched_size_has_no_row() -> None:
    chart = {"unit": "cm", "basis": "body", "rows": [{"size": "M", "chest": "100"}]}
    attrs = {"size_measurements": _fact("size_measurements", normalized=chart)}
    style = seller_facts(
        ProductFacts(product_id=uuid.uuid4(), sku="K", level="style", attributes=attrs)
    )
    assert style.size is None and not style.size_measures
    other = seller_facts(
        ProductFacts(
            product_id=uuid.uuid4(), sku="K-S", level="variant", size_key="S", attributes=attrs
        )
    )
    assert other.size == "S" and not other.size_measures and other.size_chart_unit is None
