"""The size and the product's own measurements on an Amazon item (client items 8 and 15, 12 Nov).

Pure: no HTTP, socket or database. Every attribute here is sent only where the stored Product
Type Definition the listing is checked against declares it, in the shape and with the values its
schema accepts (`schema_check.declares`, `members`, `required_members`). With no stored definition
nothing here is sent: the names are believed until `templates diff` runs on the real LISTING
definitions (DL-A), and a guessed shape would only fail Amazon's own check.

- `apparel_size(facts, schema, marketplace_id)`: Amazon's structured apparel size, one entry of
  `size_system`, `size_class`, `size` (and `body_type` / `height_type` when the definition
  requires them), from the item's size. The size system is the one the definition names for India
  (its `enumNames`, else its only value); the class is alpha for letter sizes and numeric for
  numbers; each value must be one the definition lists (aliases from `aliases.SIZE_ALIASES`). Any
  member that cannot be filled with an allowed value drops the whole attribute.
- `chart_measures(facts, schema, marketplace_id)`: the item's row of the style's saved size chart
  (item 8: chest, waist, hip, length, shoulder, sleeve, inseam) as `{value, unit}` measurements,
  each only under a `CHART_ATTRIBUTES` name the definition declares and in a unit it allows.
- `item_size(facts, schema, marketplace_id, mapped)`: the product itself (item 15), apart from the
  packed box (`mapping._package`): `item_dimensions` (length, width, height, each `{value, unit}`;
  our breadth is Amazon's width) and `item_weight` (`{value, unit}`). The dimensions go when the
  definition declares them, or, with no stored definition, when the template already maps a key
  to `item_dimensions` (the LAMP stopgap that sent the text); the weight only when the definition
  declares it (the tshirts template already maps `product_weight` -> `item_weight`).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final

from app.adapters.amazon import schema_check
from app.adapters.amazon.aliases import SIZE_ALIASES
from app.adapters.base import ProductTypeSchema, SellerFacts

APPAREL_SIZE: Final = "apparel_size"
ITEM_DIMENSIONS: Final = "item_dimensions"
ITEM_WEIGHT: Final = "item_weight"
# item 8: the chart measurement -> the Amazon attribute that may carry it (believed, DL-A)
CHART_ATTRIBUTES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "chest": "chest_size",
        "waist": "waist_size",
        "hip": "hip_size",
        "length": "item_length",
        "shoulder": "shoulder_width",
        "sleeve": "sleeve_length",
        "inseam": "inseam_length",
    }
)
LENGTH_UNITS: Final[Mapping[str, str]] = MappingProxyType({"cm": "centimeters", "in": "inches"})
INDIA: Final = ("india", "in", "ind")
ALPHA_SIZES: Final = frozenset(
    {"xxs", "xs", "s", "m", "l", "xl", "xxl", "xxxl", "xxxxl", "2xl", "3xl", "4xl", "5xl", "6xl"}
    | {"free size", "freesize", "one size", "onesize"}
)
REGULAR: Final = ("regular", "standard")
_NUMERIC: Final = re.compile(r"\d{1,3}(\.\d)?")


def _fold(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _choices(member: Any) -> tuple[tuple[str, str | None], ...] | None:
    """(value, enumName) pairs of an enum member; None when the member is free text."""
    if not isinstance(member, Mapping):
        return None
    values = member.get("enum")
    if not isinstance(values, list):
        return None
    names = member.get("enumNames")
    names = names if isinstance(names, list) and len(names) == len(values) else [None] * len(values)
    return tuple(
        (str(v), None if n is None else str(n)) for v, n in zip(values, names, strict=True)
    )


def _pick(member: Any, wanted: Sequence[str]) -> str | None:
    """The member's allowed value for the first of `wanted` matching a value or its enumName
    (folded); a free-text member takes `wanted[0]` as it is."""
    choices = _choices(member)
    if choices is None:
        if isinstance(member, Mapping) and member.get("type") == "string" and wanted:
            return wanted[0]
        return None
    for one in wanted:
        key = _fold(one)
        for value, name in choices:
            if _fold(value) == key or (name is not None and _fold(name) == key):
                return value
    return None


def _size_system(member: Any) -> str | None:
    choices = _choices(member)
    if choices is None:
        return None
    for value, name in choices:
        if name is not None and _fold(name) in INDIA:
            return value
    return choices[0][0] if len(choices) == 1 else None


def _size_class(size: str) -> str | None:
    folded = _fold(size)
    if folded in ALPHA_SIZES:
        return "alpha"
    if _NUMERIC.fullmatch(folded):
        return "numeric"
    return None


def apparel_size(
    facts: SellerFacts | None, schema: ProductTypeSchema | None, marketplace_id: str
) -> dict[str, Any] | None:
    """The `apparel_size` entry of one sellable item, or None (see the module docstring)."""
    if facts is None or not facts.size or not schema_check.declares(schema, APPAREL_SIZE):
        return None
    parts = schema_check.members(schema, APPAREL_SIZE)
    size_class = _size_class(facts.size)
    wanted_size = [facts.size, *SIZE_ALIASES.get(_fold(facts.size), ())]
    entry: dict[str, Any] = {
        "size_system": _size_system(parts.get("size_system")),
        "size_class": None if size_class is None else _pick(parts.get("size_class"), [size_class]),
        "size": _pick(parts.get("size"), wanted_size),
    }
    for member in ("body_type", "height_type"):
        if member in parts:
            entry[member] = _pick(parts.get(member), REGULAR)
    required = set(schema_check.required_members(schema, APPAREL_SIZE)) - {"marketplace_id"}
    entry = {k: v for k, v in entry.items() if v is not None and k in parts}
    if not {"size_system", "size_class", "size"} <= set(entry) or not required <= set(entry):
        return None
    entry["marketplace_id"] = marketplace_id
    return entry


def _number(value: Decimal | int) -> int | float:
    number = Decimal(value)
    return int(number) if number == number.to_integral_value() else float(number)


def _unit_allowed(member: Any, unit: str) -> bool:
    """A `{value, unit}` member (or entry) whose `unit` lists `unit` (or names no list)."""
    if not isinstance(member, Mapping):
        return False
    props = member.get("properties")
    if not isinstance(props, Mapping) or "value" not in props:
        return False
    choices = _choices(props.get("unit"))
    return choices is None or any(value == unit for value, _ in choices)


def chart_measures(
    facts: SellerFacts | None, schema: ProductTypeSchema | None, marketplace_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Amazon attribute -> `[{value, unit, marketplace_id}]` for the item's chart row."""
    chart_unit = None if facts is None else facts.size_chart_unit
    if facts is None or not facts.size_measures or chart_unit is None:
        return {}
    unit = LENGTH_UNITS.get(chart_unit)
    if unit is None:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for measure, value in sorted(facts.size_measures.items()):
        name = CHART_ATTRIBUTES.get(measure)
        if name is None or not schema_check.declares(schema, name):
            continue
        if not _unit_allowed({"properties": schema_check.members(schema, name)}, unit):
            continue
        out[name] = [{"value": _number(value), "unit": unit, "marketplace_id": marketplace_id}]
    return out


def item_size(
    facts: SellerFacts | None,
    schema: ProductTypeSchema | None,
    marketplace_id: str,
    *,
    mapped: frozenset[str] = frozenset(),
) -> dict[str, list[dict[str, Any]]]:
    """`item_dimensions` and `item_weight` of the product itself; `mapped` holds the Amazon
    attributes the pinned template already maps a key to."""
    if facts is None:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    sides = (facts.item_length_cm, facts.item_breadth_cm, facts.item_height_cm)
    declared = schema_check.declares(schema, ITEM_DIMENSIONS)
    send_dims = declared is True or (declared is None and ITEM_DIMENSIONS in mapped)
    if send_dims and all(side is not None and side > 0 for side in sides):
        parts = schema_check.members(schema, ITEM_DIMENSIONS)
        names = ("length", "width", "height")
        if declared is None or all(_unit_allowed(parts.get(n), "centimeters") for n in names):
            entry: dict[str, Any] = {
                n: {"value": _number(side), "unit": "centimeters"}
                for n, side in zip(names, sides, strict=True)
                if side is not None
            }
            entry["marketplace_id"] = marketplace_id
            out[ITEM_DIMENSIONS] = [entry]
    weight = facts.product_weight_g
    if (
        weight is not None
        and weight > 0
        and schema_check.declares(schema, ITEM_WEIGHT)
        and _unit_allowed({"properties": schema_check.members(schema, ITEM_WEIGHT)}, "grams")
    ):
        out[ITEM_WEIGHT] = [
            {"value": _number(weight), "unit": "grams", "marketplace_id": marketplace_id}
        ]
    return out
