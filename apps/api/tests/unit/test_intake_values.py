"""SI1 T3: the pure money, size chart and state helpers (AC1, AC3, AC6; CP11)."""

from __future__ import annotations

import json
from decimal import Decimal
from importlib import resources
from typing import Any

import pytest
from app.core.errors import InputError
from app.core.india import STATE_CODES
from app.services.intake_values import (
    MEASURE_KEYS,
    MoneyError,
    SizeChartError,
    check_price_le_mrp,
    normalize_size_chart,
    parse_money,
)
from mkos_schemas.codes import ErrorCode

# ---- money ----


@pytest.mark.parametrize(
    ("text", "value"),
    [("1299", Decimal("1299")), ("1299.5", Decimal("1299.5")), ("1299.00", Decimal("1299.00"))],
)
def test_money_happy(text: str, value: Decimal) -> None:
    assert parse_money(text) == value


@pytest.mark.parametrize(
    "text",
    [
        "0",
        "0.00",
        "-1",
        "1e3",
        "NaN",
        "Infinity",
        " 12",
        "12 ",
        "12.345",
        "01",
        "1" * 11,
        "١٢",  # Arabic-Indic digits
        "12\n",
        "",
    ],
)
def test_money_refused(text: str) -> None:
    with pytest.raises(MoneyError):
        parse_money(text)


@pytest.mark.parametrize("value", [12.5, 12, Decimal("12")])
def test_money_refuses_non_str(value: Any) -> None:
    with pytest.raises(MoneyError):
        parse_money(value)


def test_price_equal_to_mrp_passes() -> None:
    check_price_le_mrp(Decimal("999.00"), Decimal("999"))
    check_price_le_mrp(None, Decimal("1"))
    check_price_le_mrp(Decimal("1"), None)


def test_price_one_paisa_over_mrp_refused() -> None:
    with pytest.raises(InputError) as caught:
        check_price_le_mrp(Decimal("999.01"), Decimal("999.00"))
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.field == "selling_price"


# ---- size chart ----


def _chart(rows: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    return {"unit": "cm", "basis": "garment", "rows": rows, **over}


def test_size_chart_happy() -> None:
    chart = _chart(
        [
            {"size": "S", "chest": 96, "length": 70.5},
            {"size": "M", "chest": 101.0, "length": 72},
            {"size": "L", "chest": 106, "waist": 90},
        ]
    )
    assert normalize_size_chart(chart) == {
        "unit": "cm",
        "basis": "garment",
        "rows": [
            {"size": "S", "chest": 96, "length": 70.5},
            {"size": "M", "chest": 101, "length": 72},
            {"size": "L", "chest": 106, "waist": 90},
        ],
    }


@pytest.mark.parametrize(
    "chart",
    [
        _chart([]),
        _chart([{"size": f"S{i}", "chest": 90} for i in range(41)]),
        _chart([{"size": "m", "chest": 90}, {"size": "M", "chest": 92}]),
        _chart([{"size": "S", "bust": 90}]),
        _chart([{"size": "S", "basis": 90}]),
        _chart([{"size": "S", "chest": 0}]),
        _chart([{"size": "S", "chest": 500.1}]),
        _chart([{"size": "S", "chest": float("nan")}]),
        _chart([{"size": "S", "chest": float("inf")}]),
        _chart([{"size": "S", "chest": 90.25}]),
        _chart([{"size": "S", "chest": True}]),
        _chart([{"size": "S"}]),
        _chart([{"size": "X" * 21, "chest": 90}]),
        _chart([{"size": "", "chest": 90}]),
        _chart([{"size": "S", "chest": 90}], unit="mm"),
        _chart([{"size": "S", "chest": 90}], basis="model"),
        {"unit": "cm", "rows": [{"size": "S", "chest": 90}]},
        {**_chart([{"size": "S", "chest": 90}]), "extra": 1},
    ],
)
def test_size_chart_refused(chart: dict[str, Any]) -> None:
    with pytest.raises(SizeChartError):
        normalize_size_chart(chart)


def test_size_chart_duplicate_size() -> None:
    with pytest.raises(SizeChartError):
        normalize_size_chart(_chart([{"size": "m", "chest": 90}, {"size": "M", "chest": 92}]))


def test_size_chart_unicode_size_passes() -> None:
    size = "XL\uff08\u5927\uff09"  # XL with a full-width bracketed CJK "large"
    out = normalize_size_chart(_chart([{"size": size, "chest": 110}]))
    assert out["rows"] == [{"size": size, "chest": 110}]


def test_size_chart_bounds_pass() -> None:
    rows = [{"size": f"S{i}", "chest": 500} for i in range(40)]
    assert len(normalize_size_chart(_chart(rows))["rows"]) == 40
    assert normalize_size_chart(_chart([{"size": "X" * 20, "inseam": 0.1}]))["rows"][0] == {
        "size": "X" * 20,
        "inseam": 0.1,
    }


def test_measure_keys_are_the_dictionary_vocabulary_minus_basis() -> None:
    data = json.loads(
        (resources.files("mkos_schemas") / "data/attribute_dictionary.json").read_text("utf-8")
    )
    (entry,) = [a for a in data["attributes"] if a["key"] == "size_measurements"]
    assert set(MEASURE_KEYS) == set(entry["vocabulary"]) - {"basis"}


# ---- states ----


def test_state_codes() -> None:
    assert len(STATE_CODES) == 36
    assert {"TG", "LA", "DH", "DL", "JK", "KA"} <= STATE_CODES
    assert "OR" not in STATE_CODES
    assert all(len(c) == 2 and c.isascii() and c.isupper() for c in STATE_CODES)
