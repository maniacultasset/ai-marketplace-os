"""Client item 15 (2026-11-11): a packed box that looks wrong for its category is a warning.

The ranges are per category (apparel, lamps, the rest); a likely unit slip names what the seller
probably meant. A CSV row with such a box still imports, with a row warning. Bullet 4 (12 Nov):
past the category's hard limit (a kurta box over 30 kg) the box is refused, the message naming
the allowed range, and the CSV row fails."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.services.imports.row_model import normalize_row, validate_row
from app.services.package_sense import _num, package_errors, package_warnings
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
    out = package_warnings("tshirts", None, 25_000)
    assert out == {"package_weight": "This box weighs 25 kg. Did you mean 25 g?"}


def test_kilograms_typed_as_grams_names_the_likely_value() -> None:
    out = package_warnings("home_decor_lamps", None, 3)
    assert out == {"package_weight": "This box weighs 3 g. Did you mean 3 kg (3,000 g)?"}


def test_an_absurd_weight_with_no_likely_slip_asks_to_check() -> None:
    (message,) = package_warnings("tshirts", None, 12_345).values()
    assert message == "This box weighs 12.35 kg, which is heavy for clothing. Check the weight."
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
    out = package_warnings("tshirts", [D(140), D(120), D(30)], None)
    assert out == {
        "package_dimensions": "A 140 cm side is 1.4 m long. Did you mean 140 mm (14 cm)?"
    }


def test_metres_typed_as_centimetres() -> None:
    out = package_warnings("home_decor_lamps", [D("0.4"), D("0.4"), D("0.6")], None)
    assert out == {"package_dimensions": "A 0.4 cm side is very thin. Did you mean 0.4 m (40 cm)?"}


def test_an_absurd_side_asks_to_check() -> None:
    (message,) = package_warnings(None, [D("99999.99"), D(20), D(2)], None).values()
    assert message == "A 99,999.99 cm side is 1,000 m long, which is large. Check the size."


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
                "package_weight": "25 kg",
                "package_length_cm": "140",
                "package_breadth_cm": "120",
                "package_height_cm": "30",
            }
        ),
    )
    assert result.errors == []
    assert result.row is not None and result.row.package_weight_g == 25_000
    messages = [w.message for w in result.warnings]
    assert messages == [
        "package size: A 140 cm side is 1.4 m long. Did you mean 140 mm (14 cm)?",
        "package weight: This box weighs 25 kg. Did you mean 25 g?",
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


# ---- bullet 4: the hard limits ----


@pytest.mark.parametrize("category", ["tshirts", "ethnic_wear_kurta_sets"])
def test_a_clothing_box_over_30_kg_is_refused_with_the_range(category: str) -> None:
    out = package_errors(category, None, 30_001)
    assert out == {
        "package_weight": "A packed box for clothing weighs between 20 g and 30 kg. "
        "Check the weight is the box in grams."
    }
    assert package_errors(category, None, 30_000) == {}
    # the soft band warns and never refuses
    assert package_errors(category, None, 6_000) == {}
    assert set(package_warnings(category, None, 6_000)) == {"package_weight"}


def test_a_side_past_the_limit_is_refused_and_not_warned_too() -> None:
    sides = [D(151), D(20), D(2)]
    assert package_errors("tshirts", sides, None) == {
        "package_dimensions": "A packed box for clothing has each side between 0.3 and 150 cm. "
        "Check the size is the box in cm."
    }
    assert package_warnings("tshirts", sides, 500_000) == {}
    lamp = package_errors("home_decor_lamps", [D(301), D(40), D(40)], 100_001)
    assert set(lamp) == {"package_dimensions", "package_weight"}
    assert "a lamp" in lamp["package_weight"] and "100 kg" in lamp["package_weight"]


def test_an_unknown_category_has_no_hard_limit() -> None:
    assert package_errors("kitchen", [D("99999.99"), D(40), D(40)], 2_000_000) == {}
    assert package_errors(None, None, 2_000_000) == {}


def test_a_csv_row_past_the_hard_limit_fails() -> None:
    result = validate_row(
        brands=(),
        norm=normalize_row(
            {
                **GOOD_ROW,
                "category": "ethnic_wear_kurta_sets",
                "package_weight": "500 kg",
                "package_length_cm": "300",
                "package_breadth_cm": "250",
                "package_height_cm": "30",
            }
        ),
    )
    boxes = [e for e in result.errors if e.message.startswith("package ")]
    assert [e.error_code for e in boxes] == [ErrorCode.VALUE_NOT_ALLOWED] * 2
    assert [e.message.split(":")[0] for e in boxes] == ["package size", "package weight"]
    assert "30 kg" in boxes[1].message
    assert not [w for w in result.warnings if w.message.startswith("package ")]
