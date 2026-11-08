"""SI12 T5: the photo role, seller fields, size-chart confirm and read-model wire models
(AC2, AC4, AC8-AC12)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from app.api.v1.schemas.catalog import RequirementsOut
from app.api.v1.schemas.products import (
    FieldSourceOut,
    ImageUploadIn,
    PackageIn,
    ProductCreate,
    ProductImageOut,
    ProductOut,
    ProductPatch,
    SizeChartSuggestionOut,
)
from app.api.v1.schemas.size_chart_read import SizeChartReadOut
from pydantic import ValidationError

TENANT = uuid.uuid4()
PRODUCT = uuid.uuid4()
CHART = {"unit": "cm", "basis": "garment", "rows": [{"size": "S", "chest": 96}]}


def _key() -> str:
    return f"{TENANT}/{PRODUCT}/images/{uuid.uuid4()}"


def _create(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"sku": "TS-001", "title": "Tee", "category": "tshirts"}
    body.update(over)
    return body


def _locs(error: ValidationError) -> list[tuple[Any, ...]]:
    return [tuple(e["loc"]) for e in error.errors()]


# ---- the photo role ----


@pytest.mark.parametrize("role", ["back", "close_up"])
def test_role_on_secondary(role: str) -> None:
    upload = ImageUploadIn.model_validate(
        {"storage_key": _key(), "slot": "secondary", "role": role}
    )
    assert upload.role == role


def test_role_defaults_to_none() -> None:
    assert ImageUploadIn.model_validate({"storage_key": _key(), "slot": "main"}).role is None


@pytest.mark.parametrize("slot", ["main", "lifestyle", "size_chart", "packaging_label"])
def test_role_on_other_slot_refused_at_role(slot: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ImageUploadIn.model_validate({"storage_key": _key(), "slot": slot, "role": "back"})
    assert _locs(caught.value) == [("role",)]


@pytest.mark.parametrize("role", ["front", "", "BACK", "main"])
def test_role_vocabulary_closed(role: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ImageUploadIn.model_validate({"storage_key": _key(), "slot": "secondary", "role": role})
    assert _locs(caught.value)[0][0] == "role"


def test_image_out_role_default() -> None:
    raw = {
        "image_id": str(uuid.uuid4()),
        "slot": "secondary",
        "position": 1,
        "status": "uploaded",
        "compliance_status": "pending",
    }
    assert ProductImageOut.model_validate(raw).role is None
    assert ProductImageOut.model_validate(raw | {"role": "close_up"}).role == "close_up"


# ---- HSN, country, package ----


@pytest.mark.parametrize("hsn", ["6109", "610910", "61091000"])
def test_hsn_accepted(hsn: str) -> None:
    assert ProductCreate.model_validate(_create(hsn_code=hsn)).hsn_code == hsn
    assert ProductPatch.model_validate({"hsn_code": hsn}).hsn_code == hsn


@pytest.mark.parametrize("hsn", ["610", "61091", "6109100", "６１０９", "", 6109])  # noqa: RUF001
def test_hsn_refused(hsn: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        ProductPatch.model_validate({"hsn_code": hsn})
    assert _locs(caught.value)[0][0] == "hsn_code"


def test_country_upper_cased() -> None:
    assert ProductCreate.model_validate(_create(country_of_origin="in")).country_of_origin == "IN"
    assert ProductPatch.model_validate({"country_of_origin": "gb"}).country_of_origin == "GB"


@pytest.mark.parametrize("country", ["IND", "I", "Ä1", "", "ＩＮ"])  # noqa: RUF001
def test_country_refused(country: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ProductPatch.model_validate({"country_of_origin": country})
    assert _locs(caught.value)[0][0] == "country_of_origin"


DIMS = {"length_cm": "30", "breadth_cm": "20.5", "height_cm": 4}


def test_package_full() -> None:
    package = PackageIn.model_validate({**DIMS, "weight_g": 450})
    assert package.length_cm == Decimal("30")
    assert package.height_cm == Decimal(4)
    assert package.weight_g == 450


@pytest.mark.parametrize("value", ["0.01", "99999.99"])
def test_dimension_bounds_accepted(value: str) -> None:
    assert PackageIn.model_validate({**DIMS, "length_cm": value}).length_cm == Decimal(value)


@pytest.mark.parametrize("value", ["0", "-1", "100000", "1.234", "NaN", "Infinity"])
def test_dimension_bounds_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        PackageIn.model_validate({**DIMS, "length_cm": value})


@pytest.mark.parametrize("grams", [0, -5, 2_000_001, 1.5, True, "500"])
def test_weight_refused(grams: Any) -> None:
    with pytest.raises(ValidationError):
        PackageIn.model_validate({"weight_g": grams})


@pytest.mark.parametrize("grams", [1, 2_000_000])
def test_weight_bounds_accepted(grams: int) -> None:
    assert PackageIn.model_validate({"weight_g": grams}).weight_g == grams


def test_two_of_three_dims_refused_at_package() -> None:
    with pytest.raises(ValidationError) as caught:
        ProductPatch.model_validate({"package": {"length_cm": 1, "breadth_cm": 2}})
    assert _locs(caught.value) == [("package",)]


def test_two_null_one_set_refused() -> None:
    with pytest.raises(ValidationError):
        PackageIn.model_validate({"length_cm": None, "breadth_cm": None, "height_cm": 3})


def test_three_null_dims_accepted() -> None:
    package = PackageIn.model_validate({"length_cm": None, "breadth_cm": None, "height_cm": None})
    assert package.model_fields_set == {"length_cm", "breadth_cm", "height_cm"}


def test_weight_only_accepted() -> None:
    package = PackageIn.model_validate({"weight_g": 500})
    assert package.model_fields_set == {"weight_g"}


def test_empty_package_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        ProductPatch.model_validate({"package": {}})
    assert _locs(caught.value) == [("package",)]


def test_package_null_clears() -> None:
    patch = ProductPatch.model_validate({"package": None})
    assert "package" in patch.model_fields_set and patch.package is None


def test_package_extra_key_refused() -> None:
    with pytest.raises(ValidationError):
        PackageIn.model_validate({"weight_g": 5, "weight_kg": 1})


def test_create_takes_the_seller_fields() -> None:
    body = ProductCreate.model_validate(
        _create(hsn_code="6109", country_of_origin="in", package={**DIMS, "weight_g": 200})
    )
    assert body.package is not None and body.package.weight_g == 200


# ---- size-chart confirm ----


def test_size_chart_image_id_with_chart() -> None:
    image = uuid.uuid4()
    patch = ProductPatch.model_validate({"size_chart": CHART, "size_chart_image_id": str(image)})
    assert patch.size_chart_image_id == image


def test_size_chart_image_id_alone_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        ProductPatch.model_validate({"size_chart_image_id": str(uuid.uuid4())})
    assert _locs(caught.value) == [("size_chart_image_id",)]


def test_size_chart_image_id_with_null_chart_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        ProductPatch.model_validate({"size_chart": None, "size_chart_image_id": str(uuid.uuid4())})
    assert _locs(caught.value) == [("size_chart_image_id",)]


# ---- read models ----


def _out(**over: Any) -> dict[str, Any]:
    now = datetime(2026, 11, 8, tzinfo=UTC).isoformat()
    body: dict[str, Any] = {
        "id": str(PRODUCT),
        "sku": "TS-001",
        "title": "Tee",
        "status": "draft",
        "category": "tshirts",
        "created_at": now,
        "updated_at": now,
    }
    body.update(over)
    return body


def test_old_product_body_parses() -> None:
    out = ProductOut.model_validate(_out())
    assert out.hsn_code is None and out.package is None and out.country_of_origin is None
    assert out.field_sources == {}
    assert out.suggestions.hsn_code is None
    assert out.size_chart_suggestion is None


def test_new_product_fields_parse() -> None:
    image = str(uuid.uuid4())
    out = ProductOut.model_validate(
        _out(
            hsn_code="6109",
            country_of_origin="IN",
            package={"length_cm": "30.00", "breadth_cm": "20.00", "height_cm": "4.00"},
            field_sources={
                "color": {"kind": "photo", "image_id": image, "confidence": 0.9},
                "hsn_code": {"kind": "you"},
            },
            suggestions={"hsn_code": {"value": "6109", "basis": "category"}},
            size_chart_suggestion={"image_id": image, "status": "read", "chart": CHART},
        )
    )
    assert out.package is not None and out.package.weight_g is None
    assert out.field_sources["hsn_code"].image_id is None
    assert out.suggestions.hsn_code is not None
    assert out.size_chart_suggestion is not None and out.size_chart_suggestion.chart is not None


def test_field_source_kinds_closed() -> None:
    with pytest.raises(ValidationError):
        FieldSourceOut.model_validate({"kind": "guess"})


def test_suggestion_status_closed() -> None:
    with pytest.raises(ValidationError):
        SizeChartSuggestionOut.model_validate({"image_id": str(uuid.uuid4()), "status": "done"})


def test_read_out_and_requirements() -> None:
    read = SizeChartReadOut.model_validate({"image_id": str(uuid.uuid4()), "status": "queued"})
    assert read.job_id is None and read.poll_url is None
    req = RequirementsOut.model_validate(
        {"category": "tshirts", "amazon": ["mrp"], "flipkart": ["hsn_code"]}
    )
    assert req.flipkart == ["hsn_code"]


def test_suggestion_keeps_an_unknown_unit_and_the_line() -> None:
    chart = {"unit": "unknown", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}
    out = SizeChartSuggestionOut.model_validate(
        {"image_id": str(uuid.uuid4()), "status": "read", "chart": chart}
    )
    assert out.chart is not None and out.chart.unit == "unknown"
    assert out.unit_source_line is None
    line = {"unit_source_line": "All measurements in cm"}
    out = SizeChartSuggestionOut.model_validate(
        {"image_id": str(uuid.uuid4()), "status": "read", "chart": chart | {"unit": "cm"}} | line
    )
    assert out.unit_source_line == "All measurements in cm"


@pytest.mark.parametrize("unit", ["unknown", "mm", "", None, "CM"])
def test_saved_chart_unit_is_chosen(unit: Any) -> None:
    chart = {"unit": unit, "basis": "garment", "rows": [{"size": "M", "chest": 100}]}
    with pytest.raises(ValidationError) as caught:
        ProductPatch.model_validate({"size_chart": chart})
    (error,) = caught.value.errors()
    assert error["loc"] == ("size_chart", "unit") and error["type"] == "mkos_invalid"
    assert error["msg"] == "Choose the size chart's unit, cm or in, before saving."


def test_saved_chart_unit_has_no_default() -> None:
    chart = {"basis": "garment", "rows": [{"size": "M", "chest": 100}]}
    with pytest.raises(ValidationError) as caught:
        ProductPatch.model_validate({"size_chart": chart})
    assert caught.value.errors()[0]["type"] == "missing"
