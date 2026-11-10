"""Seller intake values: money, the price/MRP rule and the size chart (SI1 T3; AC1, AC3, CP11);
since SI12 also HSN, package size and weight and the country of origin, shared with the CSV import
(`app/services/imports/row_model.py` imports the patterns from here: one source).

Pure: no I/O. Money on the wire is a decimal string `^(0|[1-9][0-9]{0,9})(\\.[0-9]{1,2})?$` above
zero (`Numeric(12,2)`, `app/db/models/catalog.py`). The size chart is stored as
`size_measurements` `{unit, basis, rows[]}` on a style or standalone row (CP3).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.size_chart_read import (
    MAX_SIZE_CHART_MEASURE,
    MAX_SIZE_CHART_READ_ROWS,
    MAX_SIZE_CHART_SIZE_CHARS,
    SIZE_CHART_MEASURE_DECIMALS,
    SIZE_CHART_MEASURE_KEYS,
)

from app.core.errors import InputError
from app.services.iso_countries import ISO_3166_ALPHA2

__all__ = [
    "COUNTRY_PATTERN",
    "DIMENSION_PATTERN",
    "HSN_PATTERN",
    "MAX_DIMENSION",
    "MAX_GRAMS",
    "MEASURE_KEYS",
    "MONEY_PATTERN",
    "MoneyError",
    "PartyInput",
    "SizeChartError",
    "check_dimension",
    "check_grams",
    "check_hsn",
    "check_price_le_mrp",
    "inherited_money_breach",
    "normalize_country",
    "normalize_size_chart",
    "parse_money",
]

MONEY_PATTERN: Final = r"^(0|[1-9][0-9]{0,9})(\.[0-9]{1,2})?$"
_MONEY: Final = re.compile(MONEY_PATTERN)

# The seller size-chart bounds come from the seam module (`mkos_schemas.size_chart_read`), so the
# pipeline screens a read with the same ones. `MEASURE_KEYS` is the `size_measurements` vocabulary
# minus `basis` (attribute_dictionary.json; a test pins it).
MEASURE_KEYS: Final = SIZE_CHART_MEASURE_KEYS
UNITS: Final = frozenset({"cm", "in"})
# A read whose unit is not backed by a printed line keeps `unknown` (a suggestion only; never saved).
READ_UNITS: Final = UNITS | {"unknown"}
BASES: Final = frozenset({"garment", "body"})
MAX_ROWS: Final = MAX_SIZE_CHART_READ_ROWS
MAX_SIZE_CHARS: Final = MAX_SIZE_CHART_SIZE_CHARS
MAX_MEASURE: Final = MAX_SIZE_CHART_MEASURE
_MEASURE_STEP: Final = Decimal(1).scaleb(-SIZE_CHART_MEASURE_DECIMALS)

# The database's own bounds (DATABASE.md:1108-1112): HSN 4, 6 or 8 ASCII digits; dimensions
# `numeric(7,2)` > 0; whole grams 1..2,000,000; ISO 3166 alpha-2 (upper-cased). Always `fullmatch`.
HSN_PATTERN: Final = re.compile(r"[0-9]{4}|[0-9]{6}|[0-9]{8}")
DIMENSION_PATTERN: Final = re.compile(r"[0-9]{1,5}(\.[0-9]{1,2})?")
COUNTRY_PATTERN: Final = re.compile(r"[A-Za-z]{2}")
MAX_GRAMS: Final = 2_000_000
MAX_DIMENSION: Final = Decimal("99999.99")


@dataclass(frozen=True, slots=True)
class PartyInput:
    """A business party as the seller sends it (PLAN D2 `PartyIn`)."""

    legal_name: str
    address_line1: str
    city: str
    state_code: str
    postal_code: str
    country_code: str = "IN"
    address_line2: str | None = None
    gstin: str | None = None
    email: str | None = None
    phone: str | None = None

    def has_nul(self) -> bool:
        return any(
            isinstance(v, str) and "\x00" in v
            for v in (getattr(self, f.name) for f in fields(self))
        )


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


Money = tuple[Decimal | None, Decimal | None]


def inherited_money_breach(child: Money, style: Money) -> str | None:
    """The side a child's `(selling_price, mrp)` breaks when one of them is inherited from
    `style` (CP14; the row CHECK `ck_price_le_mrp` sees one row only): `selling_price` when the
    child inherits the price over its own MRP, `mrp` when it inherits an MRP under its own
    price, None when the pair holds or neither side is inherited (each row's CHECK covers it)."""
    own_price, own_mrp = child
    if (own_price is None) == (own_mrp is None):
        return None
    price = own_price if own_price is not None else style[0]
    mrp = own_mrp if own_mrp is not None else style[1]
    if price is None or mrp is None or price <= mrp:
        return None
    return "selling_price" if own_price is None else "mrp"


def _measure(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise SizeChartError("a measurement is a number")
    number = Decimal(str(value)) if not isinstance(value, Decimal) else value
    if isinstance(value, float) and not math.isfinite(value):
        raise SizeChartError("a measurement is finite")
    if not number.is_finite() or not (0 < number <= MAX_MEASURE):
        raise SizeChartError(f"a measurement is above 0 and at most {MAX_MEASURE}")
    if number != number.quantize(_MEASURE_STEP):
        raise SizeChartError("a measurement has at most one decimal")
    return int(number) if number == number.to_integral_value() else float(number)


def _size(value: object) -> str:
    if not isinstance(value, str):
        raise SizeChartError("a size is text")
    size = unicodedata.normalize("NFC", value).strip()
    if not size or len(size) > MAX_SIZE_CHARS or "\x00" in size:
        raise SizeChartError(f"a size is 1..{MAX_SIZE_CHARS} characters")
    return size


def normalize_size_chart(raw: Mapping[str, Any], *, unknown_unit: bool = False) -> dict[str, Any]:
    """The stored `{unit, basis, rows}` form, or `SizeChartError`. `unknown_unit` admits the unit
    `unknown`, only for a read kept as a suggestion: a saved chart is always `cm` or `in`."""
    if set(raw) != {"unit", "basis", "rows"}:
        raise SizeChartError("a size chart has exactly unit, basis and rows")
    unit, basis, rows = raw["unit"], raw["basis"], raw["rows"]
    if unit not in (READ_UNITS if unknown_unit else UNITS):
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


def check_hsn(value: str) -> str:
    """An HSN code of 4, 6 or 8 ASCII digits, unchanged."""
    if not HSN_PATTERN.fullmatch(value):
        raise ValueError("HSN code must be 4, 6 or 8 digits")
    return value


def normalize_country(value: str) -> str:
    """An assigned ISO 3166-1 alpha-2 country code, upper-cased."""
    if not value.isascii() or not COUNTRY_PATTERN.fullmatch(value):
        raise ValueError("country of origin must be a two-letter ISO code")
    code = value.upper()
    if code not in ISO_3166_ALPHA2:
        raise ValueError("country of origin must be an ISO 3166 country code")
    return code


def check_dimension(value: Decimal) -> Decimal:
    """A package dimension in cm: finite, above zero, at most 99999.99, at most 2 decimals."""
    exponent = value.as_tuple().exponent
    if (
        not value.is_finite()
        or not isinstance(exponent, int)
        or exponent < -2
        or not Decimal(0) < value <= MAX_DIMENSION
    ):
        raise ValueError("package dimension must be above 0 and at most 99999.99 cm")
    return value


def check_grams(value: int) -> int:
    """A package weight in whole grams, 1 to 2,000,000."""
    if not 1 <= value <= MAX_GRAMS:
        raise ValueError(f"package weight must be 1 to {MAX_GRAMS} grams")
    return value
