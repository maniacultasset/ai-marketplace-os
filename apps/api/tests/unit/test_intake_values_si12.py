"""SI12 T4 (AC9): the seller-value validators shared by the CSV import and the products API."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.services import intake_values as iv
from app.services.imports import row_model


@pytest.mark.parametrize("value", ["6109", "610910", "61091000"])
def test_hsn_accepted(value: str) -> None:
    assert iv.check_hsn(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "610",
        "61091",
        "6109100",
        "\uff16\uff11\uff10\uff19",
        "6109\n",
        "",
        " 6109",
        "61a9",
        "610910000",
    ],
)
def test_hsn_refused(value: str) -> None:
    with pytest.raises(ValueError, match="HSN"):
        iv.check_hsn(value)


@pytest.mark.parametrize(("value", "out"), [("in", "IN"), ("IN", "IN"), ("gB", "GB")])
def test_country_upper_cased(value: str, out: str) -> None:
    assert iv.normalize_country(value) == out


@pytest.mark.parametrize("value", ["IND", "I", "Ä1", "", "I1", "\uff29\uff2e", "IN\n"])
def test_country_refused(value: str) -> None:
    with pytest.raises(ValueError, match="country"):
        iv.normalize_country(value)


@pytest.mark.parametrize("value", ["0.01", "99999.99", "12", "12.5"])
def test_dimension_accepted(value: str) -> None:
    assert iv.check_dimension(Decimal(value)) == Decimal(value)


@pytest.mark.parametrize("value", ["0", "-1", "100000", "1.234", "NaN", "Infinity", "-0.00"])
def test_dimension_refused(value: str) -> None:
    with pytest.raises(ValueError, match="dimension"):
        iv.check_dimension(Decimal(value))


@pytest.mark.parametrize("value", [1, 2_000_000, 500])
def test_grams_accepted(value: int) -> None:
    assert iv.check_grams(value) == value


@pytest.mark.parametrize("value", [0, -1, 2_000_001])
def test_grams_refused(value: int) -> None:
    with pytest.raises(ValueError, match="grams"):
        iv.check_grams(value)


def test_one_source_for_import_and_api() -> None:
    imported = vars(row_model)
    for name in ("HSN_PATTERN", "DIMENSION_PATTERN", "COUNTRY_PATTERN", "MAX_GRAMS"):
        assert imported[name] is getattr(iv, name), name
    assert Decimal("99999.99") == iv.MAX_DIMENSION
