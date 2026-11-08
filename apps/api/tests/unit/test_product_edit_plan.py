"""SI1 T12: `plan_patch`, the pure change set of a product PATCH (D1b, CP13, CP14)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.errors import InputError
from app.repositories.attribute_definitions import AttributeDef
from app.services.product_edit import plan_patch
from mkos_schemas.codes import ErrorCode

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
COLOR = AttributeDef("color", "attributes", ("colour", "style", "standalone"), ())
FIT = AttributeDef("fit", "attributes", ("style", "variant", "standalone"), ())
DEFS = (COLOR, FIT)
SELLER = {"source": "seller", "captured_via": "api", "updated_at": "2026-11-01T00:00:00Z"}


def _row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "title": "Tee",
        "category": "t_shirts",
        "style_number": None,
        "brand_name": None,
        "gender": None,
        "mrp": None,
        "selling_price": None,
        "manufacturer_party_id": None,
        "parent_product_id": None,
        "variation_theme": None,
        "attributes": {"color": {"value": "red", **SELLER}},
        "provenance": {},
    }
    base.update(over)
    return SimpleNamespace(**base)


def _plan(patch: dict[str, Any], row: Any = None, **kw: Any) -> Any:
    kw.setdefault("level", "standalone")
    kw.setdefault("has_listing", False)
    return plan_patch(row or _row(), kw.pop("style", None), patch, DEFS, now=NOW, **kw)


def _refused(patch: dict[str, Any], **kw: Any) -> InputError:
    with pytest.raises(InputError) as caught:
        _plan(patch, **kw)
    return caught.value


def test_empty_patch_is_empty() -> None:
    assert _plan({}).empty


def test_same_values_are_empty() -> None:
    row = _row(mrp=Decimal("999.00"), style_number="ST-1")
    change = _plan(
        {"title": "Tee", "mrp": Decimal("999"), "style_number": "ST-1", "attributes": {}},
        row=row,
    )
    assert change.empty


def test_same_attribute_value_is_empty() -> None:
    assert _plan({"attributes": {"color": "red"}}).empty


def test_title_change_and_clear() -> None:
    change = _plan({"title": "New", "attributes": {"color": None}})
    assert change.typed_set == {"title": "New"}
    assert change.attrs_clear == ("color",)


def test_clear_of_absent_value_is_empty() -> None:
    assert _plan({"mrp": None, "style_number": None, "size_chart": None}).empty


def test_clear_mrp_names_the_key() -> None:
    assert _plan({"mrp": None}, row=_row(mrp=Decimal(1))).typed_clear == ("mrp",)


def test_null_title_refused() -> None:
    assert _refused({"title": None}).field == "title"


def test_style_number_on_variant_refused() -> None:
    error = _refused({"style_number": "X"}, level="variant")
    assert (error.code, error.field) == (ErrorCode.FACT_LEVEL_INVALID, "style_number")


def test_size_chart_on_variant_refused() -> None:
    chart = {"unit": "cm", "basis": "body", "rows": [{"size": "M", "chest": 90}]}
    error = _refused({"size_chart": chart}, level="variant")
    assert (error.code, error.field) == (ErrorCode.FACT_LEVEL_INVALID, "size_chart")


def test_attribute_outside_level_refused_on_variant() -> None:
    error = _refused({"attributes": {"color": "blue"}}, level="variant")
    assert (error.code, error.field) == (ErrorCode.FACT_LEVEL_INVALID, "attributes.color")


def test_unknown_attribute_clear_refused() -> None:
    assert _refused({"attributes": {"shade": None}}).code is ErrorCode.UNKNOWN_FACT_KEY


def test_category_with_listing_refused() -> None:
    assert _refused({"category": "kurtas"}, has_listing=True).field == "category"
    assert _plan({"category": "kurtas"}).typed_set == {"category": "kurtas"}


def test_price_above_own_mrp_refused() -> None:
    error = _refused({"mrp": Decimal("10"), "selling_price": Decimal("10.01")})
    assert error.field == "selling_price"


def test_mrp_lowered_under_own_price_named_mrp() -> None:
    row = _row(selling_price=Decimal("500"), mrp=Decimal("600"))
    assert _refused({"mrp": Decimal("499")}, row=row).field == "mrp"


def test_variant_price_above_style_mrp_refused() -> None:
    style = _row(mrp=Decimal("999"))
    row = _row(parent_product_id=uuid.uuid4())
    error = _refused({"selling_price": Decimal("1000")}, row=row, level="variant", style=style)
    assert error.field == "selling_price"


def test_style_mrp_under_child_price_refused() -> None:
    row = _row(variation_theme="size")
    error = _refused(
        {"mrp": Decimal("899")},
        row=row,
        level="style",
        children=[(Decimal("900"), None)],
    )
    assert error.field == "mrp"
    # a child with its own MRP is judged against that, not the style's
    ok = _plan(
        {"mrp": Decimal("899")}, row=row, level="style", children=[(Decimal("900"), Decimal("950"))]
    )
    assert ok.typed_set == {"mrp": Decimal("899")}


def test_style_price_raise_over_child_own_mrp_refused() -> None:
    # tester repro (round 1): the child has its own MRP and inherits the style's price
    row = _row(variation_theme="size", mrp=Decimal("1000"), selling_price=Decimal("400"))
    error = _refused(
        {"selling_price": Decimal("600")},
        row=row,
        level="style",
        children=[(None, Decimal("500"))],
    )
    assert error.field == "selling_price"
    # a child with its own price is not affected by the style's price
    ok = _plan(
        {"selling_price": Decimal("600")},
        row=row,
        level="style",
        children=[(Decimal("450"), Decimal("500"))],
    )
    assert ok.typed_set == {"selling_price": Decimal("600")}


def test_style_price_and_mrp_change_checked_against_children() -> None:
    row = _row(variation_theme="size", mrp=Decimal("1000"), selling_price=Decimal("400"))
    # both change: the inheriting-price child with its own MRP 500 gets price 700
    error = _refused(
        {"mrp": Decimal("2000"), "selling_price": Decimal("700")},
        row=row,
        level="style",
        children=[(None, Decimal("500"))],
    )
    assert error.field == "selling_price"
    # both change: the own-price child with no MRP falls under the new style MRP
    error = _refused(
        {"mrp": Decimal("800"), "selling_price": Decimal("700")},
        row=row,
        level="style",
        children=[(Decimal("900"), None)],
    )
    assert error.field == "mrp"
    ok = _plan(
        {"mrp": Decimal("2000"), "selling_price": Decimal("450")},
        row=row,
        level="style",
        children=[(None, Decimal("500")), (Decimal("900"), None)],
    )
    assert ok.typed_set == {"mrp": Decimal("2000"), "selling_price": Decimal("450")}


def test_style_price_clear_keeps_children_checked() -> None:
    # clearing the style MRP leaves an inheriting child without MRP: no comparison
    row = _row(variation_theme="size", mrp=Decimal("1000"), selling_price=Decimal("400"))
    ok = _plan({"mrp": None}, row=row, level="style", children=[(None, None)])
    assert ok.typed_clear == ("mrp",)


def test_manufacturer_needs_party_id() -> None:
    party = uuid.uuid4()
    change = _plan({"manufacturer": object()}, party_id=party)
    assert change.typed_set == {"manufacturer_party_id": party}
    row = _row(manufacturer_party_id=party)
    assert _plan({"manufacturer": object()}, row=row, party_id=party).empty
    assert _plan({"manufacturer": None}, row=row).typed_clear == ("manufacturer",)


def test_size_chart_same_table_is_empty() -> None:
    chart = {"unit": "cm", "basis": "body", "rows": [{"size": "M", "chest": 90}]}
    row = _row(attributes={"size_measurements": {"normalized": chart, **SELLER}})
    assert _plan({"size_chart": chart}, row=row).empty
    changed = {**chart, "basis": "garment"}
    assert set(_plan({"size_chart": changed}, row=row).attrs_set) == {"size_measurements"}


# ---- SI3 T15: confirm a photo value ----

NECK = AttributeDef("neckline", "attributes", ("style", "variant", "standalone"), ())
PHOTO = {
    "value": "v-neck",
    "source": "ai",
    "captured_via": "agent",
    "agent_run_id": "r",
    "confidence": 0.95,
    "evidence_ref": "image:6f1d2c3b-8a9e-4b70-9c55-2e4f6a8b0d11",
    "updated_at": "2026-11-07T00:00:00Z",
}


def _photo_row(**fact: Any) -> SimpleNamespace:
    return _row(attributes={"color": {"value": "red", **SELLER}, "neckline": {**PHOTO, **fact}})


def _confirm(patch: dict[str, Any], row: Any) -> Any:
    return plan_patch(
        row, None, patch, (*DEFS, NECK), now=NOW, level="standalone", has_listing=False
    )


def test_confirm_a_photo_value() -> None:
    change = _confirm({"confirm_attributes": ("neckline",)}, _photo_row())
    assert change.confirm == ((0, "neckline"),) and not change.empty


def test_confirm_an_already_confirmed_value_is_no_change() -> None:
    row = _photo_row(confirmed_by="u", confirmed_at="2026-11-07T00:00:00Z")
    assert _confirm({"confirm_attributes": ("neckline",)}, row).empty


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"confirm_attributes": ("color",)}, ErrorCode.VALIDATION_FAILED),  # a seller fact
        ({"confirm_attributes": ("fit",)}, ErrorCode.VALIDATION_FAILED),  # no value at all
        ({"confirm_attributes": ("sparkle",)}, ErrorCode.UNKNOWN_FACT_KEY),
        (
            {"confirm_attributes": ("neckline",), "attributes": {"neckline": "Round"}},
            ErrorCode.VALIDATION_FAILED,
        ),
        (
            {"confirm_attributes": ("neckline",), "attributes": {"neckline": None}},
            ErrorCode.VALIDATION_FAILED,
        ),
    ],
)
def test_confirm_refusals(patch: dict[str, Any], code: ErrorCode) -> None:
    with pytest.raises(InputError) as caught:
        _confirm(patch, _photo_row())
    assert caught.value.code is code and caught.value.field == "confirm_attributes[0]"


def test_confirm_refuses_other_evidence() -> None:
    row = _photo_row(evidence_ref="import_row:1", evidence_snapshot={"row": 1})
    with pytest.raises(InputError):
        _confirm({"confirm_attributes": ("neckline",)}, row)


def test_reject_is_a_clear_of_the_photo_value() -> None:
    change = _confirm({"attributes": {"neckline": None}}, _photo_row())
    assert change.attrs_clear == ("neckline",)


# ---- SI8 T9: brand and audience are style-owned typed fields ----


@pytest.mark.parametrize("level", ["style", "standalone"])
def test_brand_and_gender_planned_at_style_level(level: str) -> None:
    change = _plan({"brand_name": "Kuinn", "gender": "baby"}, level=level)
    assert change.typed_set == {"brand_name": "Kuinn", "gender": "baby"}


@pytest.mark.parametrize("name", ["brand_name", "gender"])
def test_brand_or_gender_on_a_variant_is_fact_level_invalid(name: str) -> None:
    error = _refused({name: "women"}, level="variant")
    assert (error.code, error.field) == (ErrorCode.FACT_LEVEL_INVALID, name)


def test_null_brand_and_gender_clear() -> None:
    change = _plan({"brand_name": None, "gender": None}, row=_row(brand_name="K", gender="men"))
    assert set(change.typed_clear) == {"brand_name", "gender"}


# ---- SI12 T11 (AC8): the size chart confirmed from a size-chart photo ----

CHART = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}
IMAGE = uuid.UUID("8a3f5c2e-1d4b-4e6a-9b7c-0d2e4f6a8b13")


def _chart_fact(**over: Any) -> dict[str, Any]:
    table = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}
    return {"normalized": table, **SELLER, **over}


def test_chart_with_image_id_carries_the_evidence() -> None:
    change = _plan({"size_chart": CHART}, size_chart_image_id=IMAGE)
    fact = change.attrs_set["size_measurements"]
    assert fact["evidence_ref"] == f"image:{IMAGE}"
    assert (fact["source"], fact["captured_via"]) == ("seller", "api")


def test_chart_without_image_id_is_stored_as_before() -> None:
    fact = _plan({"size_chart": CHART}).attrs_set["size_measurements"]
    assert "evidence_ref" not in fact
    assert set(fact) == {"normalized", "source", "captured_via", "updated_at"}


def test_unchanged_chart_confirmed_with_an_image_stores_the_evidence() -> None:
    row = _row(attributes={"size_measurements": _chart_fact()})
    assert _plan({"size_chart": CHART}, row=row).empty
    change = _plan({"size_chart": CHART}, row=row, size_chart_image_id=IMAGE)
    assert change.attrs_set["size_measurements"]["evidence_ref"] == f"image:{IMAGE}"


def test_resave_without_image_drops_the_stale_evidence() -> None:
    row = _row(attributes={"size_measurements": _chart_fact(evidence_ref=f"image:{IMAGE}")})
    assert _plan({"size_chart": CHART}, row=row, size_chart_image_id=IMAGE).empty
    change = _plan({"size_chart": CHART}, row=row)
    assert "evidence_ref" not in change.attrs_set["size_measurements"]


def test_evidence_counts_for_the_size_chart_only() -> None:
    row = _row(attributes={"color": {"value": "red", **SELLER, "evidence_ref": "image:x"}})
    assert _plan({"attributes": {"color": "red"}}, row=row).empty
