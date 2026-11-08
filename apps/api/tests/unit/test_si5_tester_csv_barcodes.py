"""SI5 tester (round 1): spreadsheet-shaped barcode cells in the CSV row model. Each must be a row
error with `IDENTIFIER_INVALID` (or a clean parse), never an exception or a silently changed
value."""

from __future__ import annotations

import pytest
from app.services.imports.row_model import normalize_row, validate_row
from mkos_schemas.codes import ErrorCode

# ASCII digits shifted to their full-width forms (U+FF10..U+FF19)
FULL_WIDTH = {ord(d): ord(d) + 0xFEE0 for d in "0123456789"}

GOOD: dict[str, str] = {
    "seller_sku": "KURTA-001",
    "product_name": "Cotton Kurta Set",
    "category": "ethnic_wear_kurta_sets",
    "main_image": "https://cdn.example.com/a.jpg",
}


def _run(**cells: str) -> tuple[list[ErrorCode], tuple[str | None, str | None, bool | None]]:
    result = validate_row(normalize_row({**GOOD, **cells}), brands=("Test Brand",))
    found = (
        (None, None, None)
        if result.row is None
        else (
            result.row.identifier_type,
            result.row.identifier_value,
            result.row.identifier_exemption,
        )
    )
    return [e.error_code for e in result.errors], found


@pytest.mark.parametrize(
    "cell",
    [
        "4.00638E+12",  # Excel scientific notation of 4006381333931
        "4006381333931.0",  # a float export
        "'4006381333931",  # Excel's text marker
        "4006381 333931",  # a space inside
        "4006-381-333931",
        "٤٠٠٦٣٨١٣٣٣٩٣١",  # Arabic-Indic digits
        "4006381333931".translate(FULL_WIDTH),
        "9" * 10_000,  # a huge cell
        "NaN",
        "0",
    ],
)
@pytest.mark.parametrize("column", ["ean", "barcode", "gtin"])
def test_spreadsheet_barcode_cells_are_row_errors(column: str, cell: str) -> None:
    errors, found = _run(**{column: cell})
    assert ErrorCode.IDENTIFIER_INVALID in errors, (column, cell, errors)
    assert found == (None, None, None)


@pytest.mark.parametrize(
    ("header", "cell", "expected"),
    [
        ("﻿EAN", "4006381333931", ("ean", "4006381333931")),
        (" Barcode ", "96385074", ("ean", "96385074")),
        ("UPC", "  036000291452  ", ("upc", "036000291452")),
        ("GTIN", "00036000291452", ("gtin", "00036000291452")),
    ],
)
def test_header_case_bom_and_padding(header: str, cell: str, expected: tuple[str, str]) -> None:
    errors, found = _run(**{header: cell})
    assert errors == [], errors
    assert found[:2] == expected


def test_a_barcode_header_and_the_a6_pair_conflict() -> None:
    errors, _ = _run(
        barcode="4006381333931", product_identifier_type="ean", product_identifier="4006381333931"
    )
    assert ErrorCode.CROSS_FIELD_MISMATCH in errors


@pytest.mark.parametrize("word", ["Yes", " TRUE ", "y", "1"])
def test_exemption_words_without_a_barcode(word: str) -> None:
    errors, found = _run(identifier_exemption=word)
    assert errors == [] and found == (None, None, True)


def test_an_exemption_with_a_barcode_conflicts() -> None:
    errors, found = _run(identifier_exemption="yes", ean="4006381333931")
    assert ErrorCode.CROSS_FIELD_MISMATCH in errors and found == (None, None, None)
