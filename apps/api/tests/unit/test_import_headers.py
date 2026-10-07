"""S13 B2 (DW3, AC 15-16; A§20; DATABASE.md:970, A.6): the CSV header contract."""

from __future__ import annotations

import pytest
from app.services.imports.headers import (
    A6_COLUMNS,
    ALIASES,
    GRAMS_COLUMN,
    IGNORED_WITH_WARNING,
    REQUIRED,
    check_header,
    header_report,
    normalize_header,
    row_key,
)
from hypothesis import given
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource

MINIMAL = ["seller_sku", "product_name", "category"]

# A.6 written out once more, section by section (DATABASE.md:2189-2198).
A6_LITERAL = [
    "seller_sku",
    "parent_style_id",
    "product_name",
    "brand_name",
    "manufacturer_name",
    "manufacturer_address",
    "product_identifier_type",
    "product_identifier",
    "gender",
    "category",
    "sub_category",
    "product_type",
    "color_name",
    "normalized_color",
    "size_name",
    "normalized_size",
    "size_system",
    "fit",
    "pattern",
    "fabric_type",
    "fiber_composition",
    "lining",
    "stretch",
    "neckline",
    "collar_type",
    "sleeve_type",
    "sleeve_length",
    "garment_length",
    "rise",
    "leg_style",
    "closure",
    "pocket_type",
    "occasion",
    "season",
    "wash_care",
    "iron_care",
    "country_of_origin",
    "mrp",
    "consumer_care_email",
    "consumer_care_phone",
    "hsn_code",
    "selling_price",
    "tax_rate",
    "package_weight",
    "package_length_cm",
    "package_breadth_cm",
    "package_height_cm",
    "main_image",
    "additional_images",
    "size_chart",
    "title",
    "bullet_1_to_n",
    "description",
    "keywords",
    "marketplace_category",
    "marketplace_product_type",
    "marketplace_sku",
    "marketplace_listing_id",
    "marketplace_status",
    "live_url",
    "overall_status",
    "open_issue_count",
    "human_action_required",
    "last_updated",
]


def codes(columns: list[str]) -> list[ErrorCode]:
    return [code for code, _ in check_header(columns)]


def test_a6_columns_literal_64() -> None:
    assert len(A6_COLUMNS) == 64
    assert len(set(A6_COLUMNS)) == 64
    assert list(A6_COLUMNS) == A6_LITERAL
    assert set(REQUIRED) <= set(A6_COLUMNS)
    assert set(A6_COLUMNS) >= IGNORED_WITH_WARNING


def test_check_header_minimal_ok() -> None:
    assert check_header(MINIMAL) == []
    assert check_header(list(A6_COLUMNS)) == []


@pytest.mark.parametrize("missing", REQUIRED)
def test_check_header_missing_each_required(missing: str) -> None:
    columns = [c for c in MINIMAL if c != missing]
    assert check_header(columns) == [(ErrorCode.HEADER_MISSING_COLUMN, f"missing column {missing}")]


def test_check_header_unknown() -> None:
    assert check_header([*MINIMAL, "colour"]) == [
        (ErrorCode.HEADER_UNKNOWN_COLUMN, "unknown column colour")
    ]


def test_check_header_duplicate() -> None:
    assert codes([*MINIMAL, "mrp", "mrp"]) == [ErrorCode.HEADER_DUPLICATE_COLUMN]


def test_check_header_duplicate_after_normalisation() -> None:
    problems = check_header([*MINIMAL, "MRP", " mrp "])
    assert [c for c, _ in problems] == [ErrorCode.HEADER_DUPLICATE_COLUMN]


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("parent_sku", "parent_style_id"),
        ("mrp_inr", "mrp"),
        ("price_inr", "selling_price"),
        ("package_weight_g", "package_weight"),
        ("weight_grams", "package_weight"),
        ("weight_grams", "package_weight_g"),
    ],
)
def test_check_header_alias_and_canonical_duplicate(alias: str, canonical: str) -> None:
    assert codes([*MINIMAL, canonical, alias]) == [ErrorCode.HEADER_DUPLICATE_COLUMN]


def test_check_header_bom_stripped() -> None:
    assert normalize_header("﻿seller_sku") == "seller_sku"
    assert check_header(["﻿Seller_SKU", "product_name", "category"]) == []


def test_row_model_alias_matches_after_lowercase() -> None:
    """KNOWN-ISSUES.md:47: a header that matches only after lower-casing is accepted."""
    assert row_key(normalize_header("  MRP_INR ")) == "mrp"
    assert row_key(normalize_header("Parent_SKU")) == "parent_style_id"
    assert row_key(normalize_header("PACKAGE_WEIGHT_G")) == GRAMS_COLUMN
    assert check_header(["SELLER_SKU", "Product_Name", "CATEGORY", "MRP_INR"]) == []
    assert set(ALIASES) == {
        "parent_sku",
        "mrp_inr",
        "price_inr",
        "weight_grams",
        "package_weight_g",
    }


def test_check_header_unknown_header_echo_sanitised() -> None:
    problems = check_header([*MINIMAL, "<script>alert(1)</script>", "x" * 65, "bad;col"])
    messages = [m for _, m in problems]
    assert messages == [
        "unknown column column 4",
        "unknown column column 5",
        "unknown column column 6",
    ]
    assert all("<" not in m and ";" not in m for m in messages)


def test_check_header_bullets_are_content() -> None:
    assert check_header([*MINIMAL, "bullet_1", "bullet_2"]) == []
    assert row_key("bullet_3") == "bullet_1_to_n"
    assert codes([*MINIMAL, "bullet_1", "Bullet_1"]) == [ErrorCode.HEADER_DUPLICATE_COLUMN]


def test_header_report_shape() -> None:
    report = header_report(check_header(["seller_sku", "colour"]))
    assert report.status == "failed"
    assert report.error_code is ErrorCode.IMPORT_FAILED
    assert report.retryable is False
    assert [e.error_code for e in report.errors] == [
        ErrorCode.HEADER_UNKNOWN_COLUMN,
        ErrorCode.HEADER_MISSING_COLUMN,
        ErrorCode.HEADER_MISSING_COLUMN,
    ]
    for item in report.errors:
        assert item.field == "file.header"
        assert item.source is ErrorSource.SCHEMA
        assert item.value_submitted is None
    with pytest.raises(ValueError, match="at least one"):
        header_report([])


@given(st.lists(st.text(max_size=80), max_size=80))
def test_check_header_never_raises(columns: list[str]) -> None:
    problems = check_header(columns)
    for code, message in problems:
        assert code in {
            ErrorCode.HEADER_MISSING_COLUMN,
            ErrorCode.HEADER_UNKNOWN_COLUMN,
            ErrorCode.HEADER_DUPLICATE_COLUMN,
        }
        assert message
    if problems:
        header_report(problems)
