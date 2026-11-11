"""Client item 15 (2026-11-11): a packed box that looks wrong for its category is a warning.

The ranges are per category (apparel, lamps, the rest); a likely unit slip names what the seller
probably meant. A CSV row with such a box still imports, with a row warning."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.services.imports.row_model import normalize_row, validate_row
from app.services.package_sense import _num, package_warnings
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Severity

D = Decimal
SHIRT_BOX = [D(30), D(25), D(3)]
GOOD_ROW = {
    "seller_sku": "TEE-001",
    "product_name": "Cotton T-shirt",
    "category": "tshirts",
    "main_image": "https://cdn.example.com/a.jpg",
}


@pytest.mark.parametrize(
    ("category", "sides", "grams"),
    [
        ("tshirts", SHIRT_BOX, 250),
        ("ethnic_wear_kurta_sets", [D(35), D(28), D("0.5")], 600),
        ("home_decor_lamps", [D(40), D(40), D(60)], 3_500),
        ("tshirts", None, None),
        (None, [D(10), D(10), D(10)], 1_000),
    ],
)
def test_a_sensible_box_has_no_warning(
    category: str | None, sides: list[Decimal] | None, grams: int | None
) -> None:
    assert package_warnings(category, sides, grams) == {}


def test_grams_typed_as_kilograms_names_the_likely_value() -> None:
    out = package_warnings("tshirts", None, 500_000)
    assert out == {"package_weight": "This box weighs 500 kg. Did you mean 500 g?"}


def test_kilograms_typed_as_grams_names_the_likely_value() -> None:
    out = package_warnings("home_decor_lamps", None, 3)
    assert out == {"package_weight": "This box weighs 3 g. Did you mean 3 kg (3,000 g)?"}


def test_an_absurd_weight_with_no_likely_slip_asks_to_check() -> None:
    (message,) = package_warnings("tshirts", None, 1_234_567).values()
    assert message == "This box weighs 1,234.57 kg, which is heavy for clothing. Check the weight."
    (light,) = package_warnings("home_decor_lamps", None, 40).values()
    assert light == "This box weighs only 40 g, which is light for a lamp. Check the weight."


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (D("12.50"), "12.5"),
        (D("0.255"), "0.26"),
        (999, "999"),
        (3_000, "3,000"),
        (D("1234.567"), "1,234.57"),
        (D("123456.789"), "1,23,456.79"),
        (12_345_678, "1,23,45,678"),
    ],
)
def test_numbers_read_as_the_web_form_writes_them(value: Decimal | int, text: str) -> None:
    """`toLocaleString('en-IN', {maximumFractionDigits: 2})`: the same box reads the same in a
    CSV row warning and in the form."""
    assert _num(value) == text


def test_millimetres_typed_as_centimetres() -> None:
    out = package_warnings("tshirts", [D(300), D(250), D(30)], None)
    assert out == {"package_dimensions": "A 300 cm side is 3 m long. Did you mean 300 mm (30 cm)?"}


def test_metres_typed_as_centimetres() -> None:
    out = package_warnings("home_decor_lamps", [D("0.4"), D("0.4"), D("0.6")], None)
    assert out == {"package_dimensions": "A 0.4 cm side is very thin. Did you mean 0.4 m (40 cm)?"}


def test_an_absurd_side_asks_to_check() -> None:
    (message,) = package_warnings("tshirts", [D("99999.99"), D(20), D(2)], None).values()
    assert (
        message
        == "A 99,999.99 cm side is 1,000 m long, which is large for clothing. Check the size."
    )


def test_an_unknown_category_uses_the_wide_range() -> None:
    assert package_warnings("kitchen", [D(250), D(40), D(40)], 60_000) == {}
    assert set(package_warnings("kitchen", [D(2_000), D(40), D(40)], 500_000)) == {
        "package_dimensions",
        "package_weight",
    }


def test_a_csv_row_with_an_odd_box_imports_with_warnings() -> None:
    result = validate_row(
        brands=(),
        norm=normalize_row(
            {
                **GOOD_ROW,
                "package_weight": "500 kg",
                "package_length_cm": "300",
                "package_breadth_cm": "250",
                "package_height_cm": "30",
            }
        ),
    )
    assert result.errors == []
    assert result.row is not None and result.row.package_weight_g == 500_000
    messages = [w.message for w in result.warnings]
    assert messages == [
        "package size: A 300 cm side is 3 m long. Did you mean 300 mm (30 cm)?",
        "package weight: This box weighs 500 kg. Did you mean 500 g?",
    ]
    assert {(w.severity, w.error_code) for w in result.warnings} == {
        (Severity.WARNING, ErrorCode.VALUE_NOT_ALLOWED)
    }


def test_a_csv_row_with_a_sensible_box_has_no_package_warning() -> None:
    result = validate_row(
        brands=(),
        norm=normalize_row(
            {
                **GOOD_ROW,
                "package_weight": "250 g",
                "package_length_cm": "30",
                "package_breadth_cm": "25",
                "package_height_cm": "3",
            }
        ),
    )
    assert result.errors == [] and result.warnings == []
