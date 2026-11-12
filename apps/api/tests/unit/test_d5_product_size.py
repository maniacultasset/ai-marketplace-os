"""Client item 15 (12 Nov): the product itself beside the packed box, and the box's hard limits on
the product API path.

- `item` (create and PATCH): the product's own sides (stored as the `product_dimensions` fact,
  `L x B x H cm`) and weight (`product_weight`, grams), a JSON merge patch like `package`;
- a packed box past the category's hard limit is 422 `VALUE_NOT_ALLOWED` at the field, its
  message naming the allowed range; the soft band is a read warning (`package_warnings`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.errors import domain_api_error
from app.api.v1.schemas.products import ItemSizeIn, ProductCreate, ProductPatch
from app.core.errors import InputError
from app.services.product_edit import ITEM_DIMS_KEY, ITEM_WEIGHT_COLUMN, plan_patch
from app.services.products import check_package, item_values, package_hints
from mkos_schemas.codes import ErrorCode
from pydantic import ValidationError

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
D = Decimal


def _row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "title": "Kurta",
        "category": "ethnic_wear_kurta_sets",
        "style_number": None,
        "brand_name": None,
        "gender": None,
        "mrp": None,
        "selling_price": None,
        "manufacturer_party_id": None,
        "parent_product_id": None,
        "variation_theme": None,
        "attributes": {},
        "provenance": {},
        "package_length_cm": None,
        "package_breadth_cm": None,
        "package_height_cm": None,
        "package_weight_g": None,
        "product_weight_g": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _plan(patch: dict[str, Any], row: Any = None, style: Any = None) -> Any:
    return plan_patch(
        row or _row(), style, patch, (), level="standalone", has_listing=False, now=NOW
    )


SIDES = {"length_cm": D("30"), "breadth_cm": D("15"), "height_cm": D("12.5")}


# ---- the wire model ----


def test_item_takes_the_three_sides_together_and_the_weight_alone() -> None:
    assert ItemSizeIn.model_validate({"weight_g": 420}).weight_g == 420
    ItemSizeIn.model_validate({k: str(v) for k, v in SIDES.items()})
    for bad in ({}, {"length_cm": "30"}, {**SIDES, "height_cm": None}, {"weight_g": 0}):
        with pytest.raises(ValidationError):
            ItemSizeIn.model_validate({k: str(v) if v else v for k, v in bad.items()})
    body = ProductCreate.model_validate({"sku": "K-1", "title": "Kurta", "item": {"weight_g": 420}})
    assert body.item is not None and body.item.weight_g == 420
    patch = ProductPatch.model_validate({"item": None})
    assert "item" in patch.model_fields_set and patch.item is None


# ---- the PATCH plan ----


def test_item_sides_are_the_product_dimensions_fact_and_the_weight_its_column() -> None:
    change = _plan({"item": {**SIDES, "weight_g": 420}})
    fact = change.attrs_set[ITEM_DIMS_KEY]
    assert fact["value"] == "30 x 15 x 12.5 cm" and fact["source"] == "seller"
    assert change.typed_set == {ITEM_WEIGHT_COLUMN: 420}
    assert change.groups_set == {}  # never the packed box


def test_item_merges_and_clears() -> None:
    stored = _row(
        attributes={ITEM_DIMS_KEY: {"value": "30 x 15 x 12.5 cm", "source": "seller"}},
        product_weight_g=420,
    )
    assert _plan({"item": {**SIDES}}, row=stored).empty  # the same sides
    weight_only = _plan({"item": {"weight_g": 500}}, row=stored)
    assert weight_only.typed_set == {ITEM_WEIGHT_COLUMN: 500} and not weight_only.attrs_set
    cleared = _plan({"item": None}, row=stored)
    assert cleared.attrs_clear == (ITEM_DIMS_KEY,) and cleared.typed_clear == ("product_weight",)
    sides_off = _plan({"item": dict.fromkeys(SIDES)}, row=stored)
    assert sides_off.attrs_clear == (ITEM_DIMS_KEY,) and not sides_off.typed_clear


def test_item_with_the_free_text_attribute_too_is_refused() -> None:
    with pytest.raises(InputError) as caught:
        _plan({"item": {**SIDES}, "attributes": {ITEM_DIMS_KEY: "30x15x12 cm"}})
    assert (caught.value.code, caught.value.field) == (ErrorCode.VALIDATION_FAILED, "item")


# ---- the packed box's hard limits on the API path ----


def test_a_kurta_box_over_30_kg_is_refused_at_the_weight() -> None:
    with pytest.raises(InputError) as caught:
        _plan({"package": {"weight_g": 31_000}})
    error = caught.value
    assert (error.code, error.field) == (ErrorCode.VALUE_NOT_ALLOWED, "package.weight_g")
    assert error.detail is not None and "between 20 g and 30 kg" in error.detail
    api = domain_api_error(error)
    assert api.status == 422 and api.details == [
        {"loc": ["body", "package", "weight_g"], "msg": error.detail}
    ]


def test_a_side_past_the_limit_is_refused_at_the_sides() -> None:
    with pytest.raises(InputError) as caught:
        _plan({"package": {"length_cm": D(200), "breadth_cm": D(30), "height_cm": D(5)}})
    assert caught.value.field == "package.length_cm"
    assert "150 cm" in (caught.value.detail or "")


def test_the_limit_follows_the_patched_category_and_a_variants_style() -> None:
    _plan({"package": {"weight_g": 31_000}, "category": "kitchen"})  # no hard limit there
    variant = _row(category=None, parent_product_id=uuid.uuid4())
    with pytest.raises(InputError):
        _plan({"package": {"weight_g": 31_000}}, row=variant, style=_row())
    _plan({"package": {"weight_g": 6_000}})  # the soft band saves


def test_the_create_check_uses_the_same_limits() -> None:
    with pytest.raises(InputError) as caught:
        check_package("tshirts", None, 500_000)
    assert caught.value.field == "package.weight_g"
    check_package("tshirts", (D(30), D(20), D(2)), 250)
    check_package(None, None, 500_000)


def test_the_read_warns_in_the_soft_band() -> None:
    package = {"length_cm": D(30), "breadth_cm": D(20), "height_cm": D(2), "weight_g": 6_000}
    assert set(package_hints("ethnic_wear_kurta_sets", package)) == {"package_weight"}
    assert package_hints("ethnic_wear_kurta_sets", None) == {}
    assert package_hints("tshirts", {**package, "weight_g": 250}) == {}


def test_item_values_read_own_else_the_style() -> None:
    style = _row(attributes={ITEM_DIMS_KEY: {"value": "30 x 15 x 12.5 cm"}}, product_weight_g=420)
    own = item_values(_row(product_weight_g=380), style)
    assert own == {"length_cm": D(30), "breadth_cm": D(15), "height_cm": D("12.5"), "weight_g": 380}
    assert item_values(_row(attributes={ITEM_DIMS_KEY: {"value": "a foot tall"}})) is None
    assert item_values(_row()) is None
