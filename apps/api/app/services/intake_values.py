"""Seller intake values: money, the price/MRP rule and the size chart (SI1 T3; AC1, AC3, CP11).

Pure: no I/O. Money on the wire is a decimal string `^(0|[1-9][0-9]{0,9})(\\.[0-9]{1,2})?$` above
zero (`Numeric(12,2)`, `app/db/models/catalog.py`). The size chart is stored as
`size_measurements` `{unit, basis, rows[]}` on a style or standalone row (CP3).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.core.errors import InputError

__all__ = [
    "MEASURE_KEYS",
    "MONEY_PATTERN",
    "MoneyError",
    "SizeChartError",
    "check_price_le_mrp",
    "normalize_size_chart",
    "parse_money",
]

MONEY_PATTERN: Final = r"^(0|[1-9][0-9]{0,9})(\.[0-9]{1,2})?$"
_MONEY: Final = re.compile(MONEY_PATTERN)

# `size_measurements` vocabulary minus `basis` (attribute_dictionary.json; a test pins it)
MEASURE_KEYS: Final = ("chest", "waist", "hip", "length", "shoulder", "sleeve", "inseam")
UNITS: Final = frozenset({"cm", "in"})
BASES: Final = frozenset({"garment", "body"})
MAX_ROWS: Final = 40
MAX_SIZE_CHARS: Final = 20
MAX_MEASURE: Final = 500


class MoneyError(ValueError):
    """Not a money string (CP11)."""


class SizeChartError(ValueError):
    """A refused size chart; the message names the rule, never the value."""


def parse_money(text: object) -> Decimal:
    """`text` as a positive `Decimal`, or `MoneyError`. Only a `str` is accepted."""
    if not isinstance(text, str) or _MONEY.fullmatch(text) is None:
        raise MoneyError("money is a decimal string with at most 2 decimals")
    value = Decimal(text)
    if value <= 0:
        raise MoneyError("money must be above 0")
    return value


def check_price_le_mrp(price: Decimal | None, mrp: Decimal | None) -> None:
    """The selling price is at most the MRP (`ck_price_le_mrp`; T37). Either side unset passes."""
    if price is not None and mrp is not None and price > mrp:
        raise InputError(ErrorCode.VALIDATION_FAILED, "selling_price")


def _measure(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise SizeChartError("a measurement is a number")
    number = Decimal(str(value)) if not isinstance(value, Decimal) else value
    if isinstance(value, float) and not math.isfinite(value):
        raise SizeChartError("a measurement is finite")
    if not number.is_finite() or not (0 < number <= MAX_MEASURE):
        raise SizeChartError(f"a measurement is above 0 and at most {MAX_MEASURE}")
    if number != number.quantize(Decimal("0.1")):
        raise SizeChartError("a measurement has at most one decimal")
    return int(number) if number == number.to_integral_value() else float(number)


def _size(value: object) -> str:
    if not isinstance(value, str):
        raise SizeChartError("a size is text")
    size = unicodedata.normalize("NFC", value).strip()
    if not size or len(size) > MAX_SIZE_CHARS or "\x00" in size:
        raise SizeChartError(f"a size is 1..{MAX_SIZE_CHARS} characters")
    return size


def normalize_size_chart(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The stored `{unit, basis, rows}` form, or `SizeChartError`."""
    if set(raw) != {"unit", "basis", "rows"}:
        raise SizeChartError("a size chart has exactly unit, basis and rows")
    unit, basis, rows = raw["unit"], raw["basis"], raw["rows"]
    if unit not in UNITS:
        raise SizeChartError("unit is cm or in")
    if basis not in BASES:
        raise SizeChartError("basis is garment or body")
    if not isinstance(rows, list | tuple) or not (1 <= len(rows) <= MAX_ROWS):
        raise SizeChartError(f"a size chart has 1..{MAX_ROWS} rows")
    seen: set[str] = set()
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or "size" not in row:
            raise SizeChartError("each row names its size")
        size = _size(row["size"])
        if size.casefold() in seen:
            raise SizeChartError("each size appears once")
        seen.add(size.casefold())
        out: dict[str, Any] = {"size": size}
        for key, value in row.items():
            if key == "size":
                continue
            if key not in MEASURE_KEYS:
                raise SizeChartError("a row holds only known measurements")
            if value is None:
                continue
            out[key] = _measure(value)
        if len(out) == 1:
            raise SizeChartError("each row has at least one measurement")
        out_rows.append(out)
    return {"unit": unit, "basis": basis, "rows": out_rows}
