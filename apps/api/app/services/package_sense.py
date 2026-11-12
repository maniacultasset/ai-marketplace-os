"""Client item 15 (2026-11-11): does a packed box look right for its category?

The storage limits (`intake_values.MAX_DIMENSION`, `MAX_GRAMS`) stay the hard checks. This module
adds a softer one: per category, the sides and the weight a packed box of that kind plausibly has.
A value outside the range is a warning, never an error, so an odd but true box still saves. When
the value fits the range after a likely unit slip (grams typed as kilograms, millimetres as
centimetres, metres as centimetres), the warning names the likely meaning.

`package_warnings(category, sides, weight_g)` is pure and answers `{key: message}` for
`package_dimensions` and `package_weight`. The web form mirrors it
(`src/features/products/seller-fields.ts` `packageWarnings`); keep the two in step.

Client item 15 bullet 4 (12 Nov): past the soft band a category also has a hard limit, a box no
real product of that kind ships in (a kurta box over 30 kg). `package_errors(...)` is pure and
answers `{key: message}` for the values over it; the product API (`products.create_product`,
`product_edit.update_product`) and the CSV import refuse them with 422 `VALUE_NOT_ALLOWED` at the
field, and the message names the allowed range. The soft band between the plausible range and
the hard limit stays a warning. A category with no row here keeps only the storage limits.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Final

__all__ = [
    "DEFAULT_RANGE",
    "RANGES",
    "PackageRange",
    "package_errors",
    "package_warnings",
]


@dataclass(frozen=True, slots=True)
class PackageRange:
    """A plausible packed box: each side in cm, the weight in whole grams, and what it holds."""

    min_cm: Decimal
    max_cm: Decimal
    min_g: int
    max_g: int
    noun: str | None
    # the hard limits (item 15 bullet 4): a side or a weight over these is refused, not warned
    limit_cm: Decimal | None = None
    limit_g: int | None = None


_APPAREL: Final = PackageRange(
    Decimal("0.3"), Decimal(100), 20, 5_000, "clothing", Decimal(150), 30_000
)
_LAMPS: Final = PackageRange(Decimal(1), Decimal(200), 50, 30_000, "a lamp", Decimal(300), 100_000)
DEFAULT_RANGE: Final = PackageRange(Decimal("0.1"), Decimal(300), 5, 100_000, None)
RANGES: Final[Mapping[str, PackageRange]] = MappingProxyType(
    {
        "tshirts": _APPAREL,
        "ethnic_wear_kurta_sets": _APPAREL,
        "home_decor_lamps": _LAMPS,
    }
)


def _num(value: Decimal | int) -> str:
    """12, 12.5, 0.25, 1,234.57, 1,00,000: at most two decimals, no trailing zeros, no exponent,
    digits grouped the Indian way, as the web form writes them (`toLocaleString('en-IN')`)."""
    text = format(Decimal(value).quantize(Decimal("0.01"), ROUND_HALF_UP).normalize(), "f")
    whole, dot, frac = text.partition(".")
    if len(whole) > 3:
        head, groups = whole[:-3], [whole[-3:]]
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join([head, *groups])
    return f"{whole}{dot}{frac}"


def _for(noun: str | None) -> str:
    return f" for {noun}" if noun else ""


def _weight(grams: int, r: PackageRange) -> str | None:
    if r.min_g <= grams <= r.max_g:
        return None
    if grams > r.max_g:
        kg = Decimal(grams) / 1000
        if grams % 1000 == 0 and r.min_g <= grams // 1000 <= r.max_g:
            return f"This box weighs {_num(kg)} kg. Did you mean {_num(grams // 1000)} g?"
        return f"This box weighs {_num(kg)} kg, which is heavy{_for(r.noun)}. Check the weight."
    if r.min_g <= grams * 1000 <= r.max_g:
        return (
            f"This box weighs {_num(grams)} g. "
            f"Did you mean {_num(grams)} kg ({_num(grams * 1000)} g)?"
        )
    return f"This box weighs only {_num(grams)} g, which is light{_for(r.noun)}. Check the weight."


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


def range_for(category: str | None) -> PackageRange:
    return RANGES.get((category or "").strip(), DEFAULT_RANGE)


def package_errors(
    category: str | None, sides: Sequence[Decimal] | None, weight_g: int | None
) -> dict[str, str]:
    """The hard-limit refusals of one packed box: `{key: message}` for `package_dimensions`
    (any side over the category's limit) and `package_weight`. The message gives the allowed
    range and never repeats the submitted value."""
    r = range_for(category)
    out: dict[str, str] = {}
    of = f" for {r.noun}" if r.noun else ""
    if r.limit_cm is not None and sides is not None and any(s > r.limit_cm for s in sides):
        out["package_dimensions"] = (
            f"A packed box{of} has each side between {_num(r.min_cm)} and {_num(r.limit_cm)} "
            "cm. Check the size is the box in cm."
        )
    if r.limit_g is not None and weight_g is not None and weight_g > r.limit_g:
        out["package_weight"] = (
            f"A packed box{of} weighs between {_num(r.min_g)} g and {_num(Decimal(r.limit_g) / 1000)}"
            " kg. Check the weight is the box in grams."
        )
    return out


def package_warnings(
    category: str | None, sides: Sequence[Decimal] | None, weight_g: int | None
) -> dict[str, str]:
    """The plausibility warnings of one packed box (`sides`: length, breadth and height in cm,
    all three or None). Values outside the storage limits are the hard checks' job, not this;
    a value `package_errors` refuses is not warned about as well."""
    r = range_for(category)
    hard = package_errors(category, sides, weight_g)
    out: dict[str, str] = {}
    if sides is not None and len(sides) == 3 and all(s > 0 for s in sides):
        message = _sides(sides, r)
        if message is not None and "package_dimensions" not in hard:
            out["package_dimensions"] = message
    if weight_g is not None and weight_g > 0:
        message = _weight(weight_g, r)
        if message is not None and "package_weight" not in hard:
            out["package_weight"] = message
    return out
