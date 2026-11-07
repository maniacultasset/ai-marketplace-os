"""Product barcodes (SI5, PLAN D12, D13; DATABASE.md products identifier columns): pure rules shared
by the product API and the CSV parser.

- `gtin_valid`: the GS1 check digit over GTIN-8/12/13/14, left-padded to 14 (the SQL `gtin_valid`
  twin, `0001_core_tenancy`). ASCII digits only: a full-width digit is refused.
- `TYPE_LENGTHS`: the lengths a type allows (`upc` 12, `ean` 8 or 13, `gtin` 8, 12, 13 or 14). An
  ISBN-13 is an EAN-13 (978/979) and passes as `ean`.
- `check(id_type, value)`: None when the pair is valid, else a fixed reason.
- `type_from_length(value)`: the type a bare barcode implies (12 upc, 8 or 13 ean, 14 gtin).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

GTIN_PATTERN: Final = re.compile(r"[0-9]{8}|[0-9]{12,14}")
IDENTIFIER_TYPES: Final = frozenset({"gtin", "ean", "upc"})
TYPE_LENGTHS: Final[Mapping[str, frozenset[int]]] = {
    "upc": frozenset({12}),
    "ean": frozenset({8, 13}),
    "gtin": frozenset({8, 12, 13, 14}),
}
_BY_LENGTH: Final[Mapping[int, str]] = {12: "upc", 8: "ean", 13: "ean", 14: "gtin"}

REASON_TYPE: Final = "unknown identifier type"
REASON_LENGTH: Final = "wrong length for the identifier type"
REASON_CHECK_DIGIT: Final = "not digits with a valid check digit"


def gtin_valid(value: str) -> bool:
    """The GS1 check digit over GTIN-8/12/13/14, left-padded to 14 (the SQL `gtin_valid` twin)."""
    if not isinstance(value, str) or not GTIN_PATTERN.fullmatch(value) or not value.isascii():
        return False
    digits = [int(d) for d in value.zfill(14)]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits[:13]))
    return (10 - total % 10) % 10 == digits[13]


def check(id_type: str | None, value: str | None) -> str | None:
    """None for a valid `(type, value)`, else the reason (a server constant, never the value)."""
    if id_type not in IDENTIFIER_TYPES:
        return REASON_TYPE
    if not isinstance(value, str) or not gtin_valid(value):
        return REASON_CHECK_DIGIT
    if len(value) not in TYPE_LENGTHS[id_type]:
        return REASON_LENGTH
    return None


def type_from_length(value: str) -> str | None:
    """The type a bare barcode of this length implies, or None (any other length)."""
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return None
    return _BY_LENGTH.get(len(value))
