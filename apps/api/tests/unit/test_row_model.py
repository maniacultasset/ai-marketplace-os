"""S13 B4 (DW4, AC 16; KNOWN-ISSUES.md:47; Q4, Q7): the CSV row model. One test per KI:47
defect, the DATABASE.md:1108-1112 bounds, and two properties."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest
from app.services.imports.csv_file import iter_rows
from app.services.imports.row_model import (
    ProductImportRow,
    RowResult,
    check_header,
    gtin_valid,
    normalize_row,
    validate_row,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity

IMAGE = "https://cdn.example.com/a.jpg"
GOOD: dict[str, str] = {
    "seller_sku": "KURTA-001",
    "product_name": "Cotton Kurta Set",
    "category": "ethnic_wear_kurta_sets",
    "main_image": IMAGE,
}


def run(**cells: str) -> RowResult:
    return validate_row(normalize_row({**GOOD, **cells}))


def codes(result: RowResult) -> list[ErrorCode]:
    return [e.error_code for e in result.errors]


def ok(**cells: str) -> ProductImportRow:
    result = run(**cells)
    assert result.errors == [], [e.message for e in result.errors]
    assert result.row is not None
    return result.row


# ---- one test per KNOWN-ISSUES.md:47 defect ----


def test_row_model_blank_price_is_null() -> None:
    row = ok(mrp="", selling_price="   ", tax_rate="")
    assert row.mrp is None and row.selling_price is None and row.tax_rate is None


@pytest.mark.parametrize("cell", ["NaN", "nan", "Infinity", "-Infinity", "inf"])
def test_row_model_nan_infinity_row_error(cell: str) -> None:
    result = run(mrp=cell)
    assert result.row is None
    assert codes(result) == [ErrorCode.PATTERN_MISMATCH]
    assert cell not in result.errors[0].message


def test_row_model_lowercased_header_alias() -> None:
    row = validate_row(normalize_row({**GOOD, "MRP_INR": "1299", "Price_INR": "999"})).row
    assert row is not None
    assert (row.mrp, row.selling_price) == (Decimal("1299"), Decimal("999"))
    assert check_header(["SELLER_SKU", "PRODUCT_NAME", "Category", "MRP_INR"]) == []


def test_row_model_status_always_draft() -> None:
    """A.6 has no `status` column (T69): the header is unknown and the row has no status."""
    assert [c for c, _ in check_header([*GOOD, "status"])] == [ErrorCode.HEADER_UNKNOWN_COLUMN]
    assert "status" not in normalize_row({**GOOD, "status": "active"})
    assert not hasattr(ok(), "status")


def test_row_model_short_row_blank_not_none() -> None:
    data = (
        b"seller_sku,product_name,category,main_image,mrp\nKURTA-001,Kurta,kurtas," + IMAGE.encode()
    )
    (csv_row,) = list(iter_rows(data + b"\n"))
    assert csv_row.cells["mrp"] == ""
    result = validate_row(normalize_row(csv_row.cells))
    assert result.errors == []
    assert result.row is not None and result.row.mrp is None


def test_row_model_json_looking_cell_is_plain_text() -> None:
    cell = '{"fibre": "cotton", "percent": 100, "__proto__": 1}'
    row = ok(fiber_composition=cell, fit='["a"]')
    assert row.attributes["fiber_composition"] == cell
    assert row.attributes["fit"] == '["a"]'


# ---- shapes ----


def test_row_model_currency_and_commas() -> None:
    row = ok(mrp="₹1,299.00", selling_price="INR 999.5")
    assert row.mrp == Decimal("1299.00")
    assert row.selling_price == Decimal("999.5")


def test_row_model_three_decimals_error() -> None:
    assert codes(run(mrp="12.345")) == [ErrorCode.PATTERN_MISMATCH]


def test_row_model_price_above_mrp() -> None:
    result = run(mrp="999", selling_price="1000")
    assert codes(result) == [ErrorCode.PRICE_ABOVE_MRP]
    assert ok(mrp="999", selling_price="999").selling_price == Decimal("999")


@pytest.mark.parametrize("gtin", ["96385074", "036000291452", "4006381333931", "10614141000415"])
def test_row_model_gtin_lengths(gtin: str) -> None:
    row = ok(product_identifier_type="gtin", product_identifier=gtin)
    assert row.identifier_value == gtin
    assert codes(run(product_identifier_type="ean", product_identifier="400638133393")) == [
        ErrorCode.IDENTIFIER_INVALID
    ]
    assert codes(run(product_identifier_type="ean", product_identifier="4006381333932")) == [
        ErrorCode.IDENTIFIER_INVALID
    ]


def test_row_model_gtin_leading_zeros() -> None:
    row = ok(product_identifier_type="upc", product_identifier="00012345600012")
    assert row.identifier_value == "00012345600012"
    assert gtin_valid("00012345600012")


@pytest.mark.parametrize(
    ("cell", "grams"), [("450 g", 450), ("0.45 kg", 450), ("450", None), ("2kg", 2000)]
)
def test_row_model_package_weight_units(cell: str, grams: int | None) -> None:
    result = run(package_weight=cell)
    if grams is None:
        assert codes(result) == [ErrorCode.PATTERN_MISMATCH]
        assert "unit" in result.errors[0].message
    else:
        assert result.row is not None and result.row.package_weight_g == grams


def test_row_model_package_weight_g_zero_error() -> None:
    assert codes(validate_row(normalize_row({**GOOD, "package_weight_g": "0"}))) == [
        ErrorCode.VALUE_NOT_ALLOWED
    ]
    row = validate_row(normalize_row({**GOOD, "weight_grams": "450"})).row
    assert row is not None and row.package_weight_g == 450


def test_row_model_unicode_title() -> None:
    assert ok(product_name="कुर्ता सेट · Cotton").title == "कुर्ता सेट · Cotton"


def test_row_model_whitespace_collapsed() -> None:
    row = ok(product_name="  Cotton \t  Kurta\n Set ", seller_sku=" KURTA-001 ")
    assert row.title == "Cotton Kurta Set"
    assert row.sku == "KURTA-001"


@pytest.mark.parametrize("sku", ["A]B", "x" * 65, "A\ufeffB", "A\u00a0B", "A\u3000B", "A\u2028B"])
def test_row_model_bad_sku(sku: str) -> None:
    result = run(seller_sku=sku)
    assert result.row is None
    assert codes(result)[0] in {ErrorCode.PATTERN_MISMATCH, ErrorCode.FIELD_TOO_LONG}


@settings(max_examples=300)
@given(st.text(min_size=1, max_size=64))
def test_row_model_sku_never_wider_than_the_api(sku: str) -> None:
    # A CSV SKU is later returned by the products API, so the CSV refuses whatever its `Sku` refuses
    # (U+FEFF included: the contract pattern is ECMA-262, where `\s` covers it).
    from app.api.v1.schemas.products import Sku
    from pydantic import TypeAdapter, ValidationError

    result = run(seller_sku=sku)
    if result.row is None:
        return
    try:
        TypeAdapter(Sku).validate_python(result.row.sku)
    except ValidationError as exc:  # pragma: no cover - the failure message is the point
        raise AssertionError(f"CSV accepted {result.row.sku!r}, the API refuses it") from exc


def test_row_model_parent_sku_refuses_bom() -> None:
    result = run(parent_style_id="STYLE\ufeff1")
    assert ErrorCode.PATTERN_MISMATCH in codes(result)


def test_row_model_media_http_rejected() -> None:
    result = run(main_image="http://cdn.example.com/a.jpg")
    assert ErrorCode.PATTERN_MISMATCH in codes(result)


def test_row_model_media_over_9() -> None:
    many = "|".join(f"https://cdn.example.com/{n}.jpg" for n in range(9))
    assert codes(run(additional_images=many)) == [ErrorCode.VALUE_NOT_ALLOWED]
    eight = "|".join(f"https://cdn.example.com/{n}.jpg" for n in range(8))
    assert len(ok(additional_images=eight).image_urls) == 9


def test_row_model_content_column_warning() -> None:
    result = run(title="Marketplace title", keywords="kurta")
    assert result.errors == []
    assert [w.severity for w in result.warnings] == [Severity.WARNING, Severity.WARNING]
    assert all("Marketplace title" not in w.message for w in result.warnings)


def test_row_model_errors_file_row_schema_no_value() -> None:
    canary = "CANARY-7f3e"
    result = run(mrp=canary, gender=canary, hsn_code=canary, country_of_origin=canary)
    assert len(result.errors) == 4
    for item in result.errors:
        assert item.field == "file.row"
        assert item.source is ErrorSource.SCHEMA
        assert item.value_submitted is None
        assert canary not in item.message and canary.lower() not in item.message


def test_row_model_requires_one_image() -> None:
    for cells in ({"main_image": ""}, {"main_image": "  ", "additional_images": ""}):
        assert ErrorCode.REQUIRED_FIELD_MISSING in codes(run(**cells))
    no_cell = validate_row(normalize_row({k: v for k, v in GOOD.items() if k != "main_image"}))
    assert codes(no_cell) == [ErrorCode.REQUIRED_FIELD_MISSING]
    assert "main_image" in no_cell.errors[0].message
    http_only = run(main_image="http://cdn.example.com/a.jpg")
    assert ErrorCode.REQUIRED_FIELD_MISSING in codes(http_only)
    row = ok(main_image="", additional_images="https://cdn.example.com/b.jpg")
    assert row.image_urls == ("https://cdn.example.com/b.jpg",)


@pytest.mark.parametrize(
    ("cell", "valid"),
    [
        ("0", False),
        ("-1", False),
        ("0.001", False),
        ("1e11", False),
        ("12345678901", False),
        ("9999999999.99", True),
    ],
)
def test_row_model_price_bounds(cell: str, valid: bool) -> None:
    result = run(mrp=cell)
    assert (result.errors == []) is valid


@pytest.mark.parametrize(("cell", "valid"), [("-0.01", False), ("100.01", False), ("18", True)])
def test_row_model_tax_rate_bounds(cell: str, valid: bool) -> None:
    assert (run(tax_rate=cell).errors == []) is valid


@pytest.mark.parametrize(
    ("cell", "valid"),
    [("1234", True), ("123456", True), ("12345678", True), ("12345", False), ("12a4", False)],
)
def test_row_model_hsn_pattern(cell: str, valid: bool) -> None:
    assert (run(hsn_code=cell).errors == []) is valid


@pytest.mark.parametrize("case", ["0", "100000", "all_or_none"])
def test_row_model_dimension_bounds(case: str) -> None:
    if case == "all_or_none":
        result = run(package_length_cm="10", package_breadth_cm="10")
        assert codes(result) == [ErrorCode.CROSS_FIELD_MISMATCH]
        return
    result = run(package_length_cm=case, package_breadth_cm="10", package_height_cm="10")
    assert result.row is None
    assert ok(package_length_cm="99999.99", package_breadth_cm="1", package_height_cm="0.5")


# ---- properties ----


@settings(max_examples=300)
@given(st.dictionaries(st.sampled_from([*GOOD, "mrp", "selling_price", "tax_rate", "hsn_code",
                                         "package_weight", "package_length_cm", "gender",
                                         "product_identifier", "product_identifier_type",
                                         "additional_images", "country_of_origin", "zzz"]),
                       st.text(max_size=40), max_size=14))  # fmt: skip
def test_validate_row_never_raises(cells: dict[str, str]) -> None:
    result = validate_row(normalize_row(cells))
    assert (result.row is None) == bool(result.errors)
    assert all(e.value_submitted is None for e in result.errors)


# A literal copy of the DATABASE.md:1108-1112 CHECKs and precisions (0001_core_tenancy).
def _numeric_fits(value: Decimal, precision: int, scale: int) -> bool:
    """`numeric(precision, scale)` stores it without rounding or overflow."""
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    places = max(0, -exponent)
    whole = len(str(int(abs(value)))) if abs(value) >= 1 else 0
    return value.is_finite() and places <= scale and whole <= precision - scale


def _fits_db(row: ProductImportRow) -> bool:
    checks: list[bool] = [
        re.fullmatch(r"[^\]\s]{1,64}", row.sku) is not None,
        1 <= len(row.title) <= 500,
        re.fullmatch(r"[a-z0-9_]{2,64}", row.category) is not None,
        row.mrp is None or (row.mrp > 0 and _numeric_fits(row.mrp, 12, 2)),
        row.selling_price is None
        or (row.selling_price > 0 and _numeric_fits(row.selling_price, 12, 2)),
        row.mrp is None or row.selling_price is None or row.selling_price <= row.mrp,
        row.tax_rate is None or (0 <= row.tax_rate <= 100 and _numeric_fits(row.tax_rate, 5, 2)),
        row.hsn_code is None
        or re.fullmatch(r"[0-9]{4}|[0-9]{6}|[0-9]{8}", row.hsn_code) is not None,
        row.country_of_origin is None
        or re.fullmatch(r"[A-Z]{2}", row.country_of_origin) is not None,
        row.package_weight_g is None or 0 < row.package_weight_g <= 2_147_483_647,
        (row.package_length_cm is None)
        == (row.package_breadth_cm is None)
        == (row.package_height_cm is None),
        all(
            v is None or (v > 0 and _numeric_fits(v, 7, 2))
            for v in (row.package_length_cm, row.package_breadth_cm, row.package_height_cm)
        ),
        row.gender is None or row.gender in {"men", "women", "unisex", "boys", "girls"},
        (row.identifier_type is None) == (row.identifier_value is None),
        row.identifier_value is None or gtin_valid(row.identifier_value),
    ]
    return all(checks)


_NUMBERISH = st.one_of(
    st.decimals(allow_nan=True, allow_infinity=True, places=None).map(str),
    st.from_regex(r"[0-9]{1,13}(\.[0-9]{0,4})?", fullmatch=True),
    st.sampled_from(["₹1,299.00", "0", "100", "100.00", "0.45 kg", "450 g", "-5"]),
)


@settings(max_examples=400)
@given(
    mrp=_NUMBERISH,
    price=_NUMBERISH,
    tax=_NUMBERISH,
    dim=_NUMBERISH,
    weight=_NUMBERISH,
    hsn=st.from_regex(r"[0-9a]{3,9}", fullmatch=True),
)
def test_valid_row_fits_db_columns(
    mrp: str, price: str, tax: str, dim: str, weight: str, hsn: str
) -> None:
    cells: dict[str, Any] = {
        "mrp": mrp,
        "selling_price": price,
        "tax_rate": tax,
        "hsn_code": hsn,
        "package_length_cm": dim,
        "package_breadth_cm": dim,
        "package_height_cm": dim,
        "package_weight": weight,
    }
    result = run(**cells)
    if result.row is not None:
        assert _fits_db(result.row)
