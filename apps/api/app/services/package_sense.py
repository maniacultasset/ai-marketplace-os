"""Client item 15 (2026-11-11): does a packed box look right for its category?

The storage limits (`intake_values.MAX_DIMENSION`, `MAX_GRAMS`) stay the hard checks. This module
adds a softer one: per category, the sides and the weight a packed box of that kind plausibly has.
A value outside the range is a warning, never an error, so an odd but true box still saves. When
the value fits the range after a likely unit slip (grams typed as kilograms, millimetres as
centimetres, metres as centimetres), the warning names the likely meaning.

`package_warnings(category, sides, weight_g)` is pure and answers `{key: message}` for
`package_dimensions` and `package_weight`. The web form mirrors it
(`src/features/products/seller-fields.ts` `packageWarnings`); keep the two in step.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final

__all__ = ["DEFAULT_RANGE", "RANGES", "PackageRange", "package_warnings"]


@dataclass(frozen=True, slots=True)
class PackageRange:
    """A plausible packed box: each side in cm, the weight in whole grams, and what it holds."""

    min_cm: Decimal
    max_cm: Decimal
    min_g: int
    max_g: int
    noun: str | None


_APPAREL: Final = PackageRange(Decimal("0.3"), Decimal(100), 20, 5_000, "clothing")
_LAMPS: Final = PackageRange(Decimal(1), Decimal(200), 50, 30_000, "a lamp")
DEFAULT_RANGE: Final = PackageRange(Decimal("0.1"), Decimal(300), 5, 100_000, None)
RANGES: Final[Mapping[str, PackageRange]] = MappingProxyType(
    {
        "tshirts": _APPAREL,
        "ethnic_wear_kurta_sets": _APPAREL,
        "home_decor_lamps": _LAMPS,
    }
)


def _num(value: Decimal) -> str:
    """12, 12.5, 0.25: no trailing zeros, no exponent."""
    return format(value.quantize(Decimal("0.01")).normalize(), "f")


def _for(noun: str | None) -> str:
    return f" for {noun}" if noun else ""


def _weight(grams: int, r: PackageRange) -> str | None:
    if r.min_g <= grams <= r.max_g:
        return None
    if grams > r.max_g:
        kg = Decimal(grams) / 1000
        if grams % 1000 == 0 and r.min_g <= grams // 1000 <= r.max_g:
            return f"This box weighs {_num(kg)} kg. Did you mean {grams // 1000} g?"
        return f"This box weighs {_num(kg)} kg, which is heavy{_for(r.noun)}. Check the weight."
    if r.min_g <= grams * 1000 <= r.max_g:
        return f"This box weighs {grams} g. Did you mean {grams} kg ({grams * 1000} g)?"
    return f"This box weighs only {grams} g, which is light{_for(r.noun)}. Check the weight."


def _sides(sides: Sequence[Decimal], r: PackageRange) -> str | None:
    big = max(sides)
    small = min(sides)
    if big > r.max_cm:
        if all(r.min_cm <= s / 10 <= r.max_cm for s in sides):
            return (
                f"A {_num(big)} cm side is {_num(big / 100)} m long. "
                f"Did you mean {_num(big)} mm ({_num(big / 10)} cm)?"
            )
        return (
            f"A {_num(big)} cm side is {_num(big / 100)} m long, which is large{_for(r.noun)}. "
            "Check the size."
        )
    if small < r.min_cm:
        if all(r.min_cm <= s * 100 <= r.max_cm for s in sides):
            return (
                f"A {_num(small)} cm side is very thin. "
                f"Did you mean {_num(small)} m ({_num(small * 100)} cm)?"
            )
        return f"A {_num(small)} cm side is very thin{_for(r.noun)}. Check the size is in cm."
    return None


def package_warnings(
    category: str | None, sides: Sequence[Decimal] | None, weight_g: int | None
) -> dict[str, str]:
    """The plausibility warnings of one packed box (`sides`: length, breadth and height in cm,
    all three or None). Values outside the storage limits are the hard checks' job, not this."""
    r = RANGES.get((category or "").strip(), DEFAULT_RANGE)
    out: dict[str, str] = {}
    if sides is not None and len(sides) == 3 and all(s > 0 for s in sides):
        message = _sides(sides, r)
        if message is not None:
            out["package_dimensions"] = message
    if weight_g is not None and weight_g > 0:
        message = _weight(weight_g, r)
        if message is not None:
            out["package_weight"] = message
    return out
