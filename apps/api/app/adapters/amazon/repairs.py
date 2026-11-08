"""Amazon's adapter-side repair rules (SI11 T9-T10; ADR-0028 proposed; P:B2-B3, D8-D9; spec L1061
"Auto-Fixable Classes"). Pure: no I/O, no clock, no model call (invariant 2).

`propose(listing, unit, item, attribute, ctx)` returns at most one `Repair` per issue, from a
closed rule list, or None. A repair changes one marketplace attribute's value and never the
listing's content or a seller fact: text, keywords, price, MRP and the manufacturer are refused
whatever the rule (`REFUSED_ATTRIBUTES`; the user's resend rule: only non-content fixes resend
on their own).

- `enum_fold`: the sent value folds (NFKC, strip, casefold, collapsed spaces) to exactly one value
  of the attribute's marketplace enum; else the alias table's targets in the enum must be exactly
  one (then the repair records `ALIASES_VERSION`). Zero or two matches: None (never "nearest").
- `unit_format`: the number and unit re-rendered as `{value, unit}` when the unit is one the
  marketplace names for the attribute; NaN, infinity or a negative number: None.
- `identifier_normalize`: the seller's barcode with spaces and hyphens removed, only when the
  digits pass the GTIN check digit.
- `exemption_flag`: the seller's GTIN exemption, sent as the exemption attribute.
- `fill_from_facts`: a missing attribute filled from a seller fact (`FILL`); a blank fact: None.
- `drop_locked`: Amazon keeps the first value of an attribute it locks (4005033): leave it out.
- `image_slot`: another approved image that passed our main rules becomes the main image.

`apply_repairs(attributes, listing, unit)` (the end of `item_payload`) applies each live repair of
the unit while the seller's source value still hashes to the repair's `source_digest`; any other
repair is reported stale and never applied.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from uuid import UUID

from mkos_schemas.errors import ErrorItem

from app.adapters.amazon import constants as c
from app.adapters.amazon.aliases import (
    ALIASES_VERSION,
    COLOUR_ATTRIBUTES,
    COLOUR_FAMILY_ALIASES,
    SIZE_ALIASES,
    SIZE_ATTRIBUTES,
)
from app.adapters.base import AdapterItem, AdapterListing, AdapterRepair

LOCKED_CODE: Final = "4005033"
REFUSED_ATTRIBUTES: Final = frozenset(
    {
        *c.CONTENT_FIELDS.values(),
        c.KEYWORD_ATTRIBUTE,
        c.OFFER_ATTRIBUTE,
        c.CONDITION_ATTRIBUTE,
        c.MANUFACTURER_CONTACT,
        "manufacturer",
        *c.VARIATION_ATTRIBUTES,
    }
)
# attribute -> the seller fact path that fills it (CP-D11). SI8's identity attributes join when
# its `AUDIENCE` map is present (`constants.AUDIENCE`); without it only the origin is filled.
_AUDIENCE: Final[Mapping[str, Mapping[str, str]]] = getattr(c, "AUDIENCE", {})
FILL: Final[Mapping[str, str]] = {
    "country_of_origin": "facts.country_of_origin",
    **({"brand": "facts.brand_name"} if _AUDIENCE else {}),
    **(
        {
            "target_gender": "facts.gender",
            "department": "facts.gender",
            "age_range_description": "facts.gender",
        }
        if _AUDIENCE
        else {}
    ),
}
UNITS: Final[Mapping[str, str]] = {
    "cm": "centimeters",
    "centimeter": "centimeters",
    "centimeters": "centimeters",
    "mm": "millimeters",
    "millimeters": "millimeters",
    "m": "meters",
    "meters": "meters",
    "in": "inches",
    "inch": "inches",
    "inches": "inches",
    "g": "grams",
    "grams": "grams",
    "kg": "kilograms",
    "kilograms": "kilograms",
}
_MEASURE: Final = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*$")
_SPACES: Final = re.compile(r"\s+")


@dataclass(frozen=True)
class Repair:
    rule: str
    attribute: str
    field_path: str
    value: tuple[Mapping[str, Any], ...] | None
    source_digest: bytes
    to_digest: bytes
    seller_sku: str | None = None
    alias_version: int | None = None


@dataclass(frozen=True)
class RepairContext:
    """What a rule may read besides the listing: the marketplace's enums and units per
    attribute (from the stored Product Type Definition, else the pinned template), whether Amazon
    already holds the SKU, and the approved images that pass our main rules, with their public
    locators (only after a submission: a preview sends no image, CP-D23)."""

    enums: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    units: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    stored: bool = False
    main_ok_images: tuple[tuple[UUID, str], ...] = ()
    marketplace_id: str = "A21TJRUUN4KGV"


def fold(value: str) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", value).strip().casefold())


def digest(value: Any) -> bytes:
    """sha256 of the canonical JSON of a value (sorted keys, compact, UTF-8)."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).digest()


def source_value(listing: AdapterListing, unit: AdapterItem, field_path: str) -> Any:
    """The seller-side value a repair stands on: the canonical attribute, the seller fact or the
    image the field path names (None when absent)."""
    from app.adapters.amazon.mapping import unit_attributes

    if field_path.startswith("attributes."):
        return unit_attributes(listing, unit).get(field_path.removeprefix("attributes."))
    if field_path.startswith("images["):
        index = int(field_path[len("images[") : -1])
        images = listing.listing.images
        return str(images[index].image_id) if index < len(images) else None
    facts = listing.unit_facts.get(unit.product_id)
    if field_path.startswith("facts.") and facts is not None:
        key = field_path.removeprefix("facts.")
        if key == "identifier":
            return [facts.identifier_type, facts.identifier_value, facts.identifier_exempt]
        return getattr(facts, key, None)
    return None


def ptd_enum(schema: Mapping[str, Any], attribute: str) -> tuple[str, ...]:
    """The `value` enum of an attribute in a Product Type Definition schema, or ()."""
    node = schema.get("properties", {}).get(attribute, {})
    value = node.get("items", {}).get("properties", {}).get("value", {})
    found = list(value.get("enum") or [])
    for option in value.get("anyOf") or []:
        found += list(option.get("enum") or [])
    return tuple(dict.fromkeys(str(v) for v in found))


def ptd_units(schema: Mapping[str, Any], attribute: str) -> tuple[str, ...]:
    """The `unit` enum of an attribute in a Product Type Definition schema, or ()."""
    node = schema.get("properties", {}).get(attribute, {})
    unit = node.get("items", {}).get("properties", {}).get("unit", {})
    found = list(unit.get("enum") or [])
    for option in unit.get("anyOf") or []:
        found += list(option.get("enum") or [])
    return tuple(dict.fromkeys(str(v) for v in found))


# The executor's way from an issue to the attributes a rule may repair, where `attributes_for`
# (the mapping's reverse index) has none: the barcode fact feeds the identifier or the exemption
# attribute; an issue on `listing` (Amazon named an attribute no field path feeds) names its
# attribute in the marketplace's text.
FIELD_ALIASES: Final[Mapping[str, tuple[str, ...]]] = {
    "facts.identifier": (c.EXTERNAL_ID_ATTRIBUTE, c.EXEMPTION_ATTRIBUTE),
}
_WORD: Final = re.compile(r"[a-z0-9_]{1,128}")


def repair_names(
    listing: AdapterListing, field_path: str, attributes: Mapping[str, Any], text: str
) -> list[str]:
    """The attributes `propose` may try for an issue on `field_path` of a unit sent with
    `attributes`: the reverse index, else the aliases, else (on `listing`) each attribute of the
    payload, of `FILL` or of the barcode that the issue's text names as a whole word."""
    names = attributes_for(listing, field_path)
    if names:
        return names
    if field_path in FIELD_ALIASES:
        return list(FIELD_ALIASES[field_path])
    if field_path != "listing":
        return []
    named = set(_WORD.findall(text.casefold()))
    known = {*attributes, *FILL, c.EXTERNAL_ID_ATTRIBUTE, c.EXEMPTION_ATTRIBUTE}
    return sorted(n for n in known if n in named)


def _sent(attributes: Mapping[str, Any], attribute: str, key: str = "value") -> Any:
    values = attributes.get(attribute)
    if isinstance(values, list) and values and isinstance(values[0], Mapping):
        return values[0].get(key)
    return None


def _with_value(
    attributes: Mapping[str, Any], attribute: str, new: Any
) -> tuple[dict[str, Any], ...]:
    entries = attributes.get(attribute) or []
    return tuple({**dict(e), "value": new} for e in entries if isinstance(e, Mapping)) or (
        {"value": new},
    )


def _one(matches: Sequence[str]) -> str | None:
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _enum_fold(sent: Any, enum: Sequence[str], attribute: str) -> tuple[str, int | None] | None:
    if not isinstance(sent, str) or not sent.strip() or not enum:
        return None
    folded = fold(sent)
    exact = _one([e for e in enum if fold(e) == folded])
    if exact is not None:
        return (exact, None) if exact != sent else None
    table: Mapping[str, tuple[str, ...]] = {}
    if attribute in SIZE_ATTRIBUTES:
        table = SIZE_ALIASES
    elif attribute in COLOUR_ATTRIBUTES:
        table = COLOUR_FAMILY_ALIASES
    targets = table.get(folded, ())
    by_fold = {fold(e): e for e in enum}
    hit = _one([by_fold[fold(t)] for t in targets if fold(t) in by_fold])
    if hit is None or hit == sent:
        return None
    return hit, ALIASES_VERSION


def _unit_format(sent: Any, units: Sequence[str]) -> dict[str, Any] | None:
    if not isinstance(sent, str):
        return None
    found = _MEASURE.match(sent)
    if found is None:
        return None
    try:
        number = Decimal(found.group(1))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or math.isnan(float(number)):
        return None
    unit = UNITS.get(found.group(2).casefold())
    if unit is None or unit not in units:
        return None
    return {"value": float(number), "unit": unit}


def gtin_ok(digits: str) -> bool:
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def propose(
    listing: AdapterListing,
    unit: AdapterItem,
    item: ErrorItem,
    attribute: str,
    attributes: Mapping[str, Any],
    ctx: RepairContext,
) -> Repair | None:
    """At most one repair for `item` on `attribute` of `unit`, given the attributes the unit was
    sent with (module docstring)."""
    if attribute in REFUSED_ATTRIBUTES or not re.fullmatch(r"[a-z0-9_]{1,128}", attribute):
        return None
    path = item.field
    sku = unit.seller_sku if listing.items else None
    mid = ctx.marketplace_id

    def made(
        rule: str, value: tuple[Mapping[str, Any], ...] | None, where: str, alias: int | None = None
    ) -> Repair:
        return Repair(
            rule=rule,
            attribute=attribute,
            field_path=where,
            value=value,
            source_digest=digest(source_value(listing, unit, where)),
            to_digest=digest(None if value is None else [dict(v) for v in value]),
            seller_sku=sku,
            alias_version=alias,
        )

    if item.raw_code == LOCKED_CODE:
        if ctx.stored and attribute in attributes:
            return made("drop_locked", None, path)
        return None
    if attribute == c.MAIN_IMAGE:
        sent = _sent(attributes, attribute, "media_location")
        for _image_id, locator in ctx.main_ok_images:
            if locator != sent:
                return made(
                    "image_slot", ({"media_location": locator, "marketplace_id": mid},), "images[0]"
                )
        return None
    if attribute == c.EXTERNAL_ID_ATTRIBUTE:
        entry = (attributes.get(attribute) or [None])[0]
        if not isinstance(entry, Mapping):
            return None
        digits = re.sub(r"[\s-]", "", str(entry.get("value", "")))
        if digits == entry.get("value") or not gtin_ok(digits):
            return None
        return made("identifier_normalize", ({**dict(entry), "value": digits},), "facts.identifier")
    if attribute == c.EXEMPTION_ATTRIBUTE:
        facts = listing.unit_facts.get(unit.product_id)
        if facts is None or not facts.identifier_exempt or attribute in attributes:
            return None
        return made("exemption_flag", ({"value": True, "marketplace_id": mid},), "facts.identifier")
    if attribute not in attributes and attribute in FILL:
        fact_path = FILL[attribute]
        value = source_value(listing, unit, fact_path)
        if isinstance(value, str) and attribute in (
            "target_gender",
            "department",
            "age_range_description",
        ):
            value = _AUDIENCE.get(attribute, {}).get(value.casefold())
        if not isinstance(value, str) or not value.strip():
            return None
        return made("fill_from_facts", ({"value": value, "marketplace_id": mid},), fact_path)
    if attribute not in attributes:
        return None
    sent = _sent(attributes, attribute)
    units = ctx.units.get(attribute, ())
    if units:
        formatted = _unit_format(sent, units)
        if formatted is not None:
            entries = attributes.get(attribute) or [{}]
            base = {k: v for k, v in dict(entries[0]).items() if k not in ("value", "unit")}
            return made("unit_format", ({**base, **formatted},), path)
    folded = _enum_fold(sent, ctx.enums.get(attribute, ()), attribute)
    if folded is not None:
        value, alias = folded
        return made("enum_fold", _with_value(attributes, attribute, value), path, alias)
    return None


@dataclass(frozen=True)
class Applied:
    attributes: dict[str, Any]
    repaired: tuple[tuple[UUID, str, bytes, bytes], ...]
    """(repair id, field path, from digest: the attribute as built, to digest)."""
    stale: tuple[UUID, ...]


def apply_repairs(
    attributes: Mapping[str, Any], listing: AdapterListing, unit: AdapterItem
) -> Applied:
    """The unit's attributes with each matching live repair applied (module docstring)."""
    out = dict(attributes)
    repaired: list[tuple[UUID, str, bytes, bytes]] = []
    stale: list[UUID] = []
    for repair in listing.repairs:
        if repair.attribute in REFUSED_ATTRIBUTES:
            continue  # content, offer and seller-owned attributes never change without a human
        if repair.seller_sku is not None and repair.seller_sku != unit.seller_sku:
            continue
        if digest(source_value(listing, unit, repair.field_path)) != repair.source_digest:
            stale.append(repair.id)
            continue
        before = digest(out.get(repair.attribute))
        if repair.value is None:
            out.pop(repair.attribute, None)
        else:
            out[repair.attribute] = [dict(v) for v in repair.value]
        after = digest(out.get(repair.attribute))
        if after != before:
            repaired.append((repair.id, repair.field_path, before, repair.to_digest))
    return Applied({k: out[k] for k in sorted(out)}, tuple(repaired), tuple(stale))


def as_adapter_repair(repair: Repair, repair_id: UUID) -> AdapterRepair:
    return AdapterRepair(
        id=repair_id,
        field_path=repair.field_path,
        attribute=repair.attribute,
        rule=repair.rule,
        source_digest=repair.source_digest,
        to_digest=repair.to_digest,
        value=repair.value,
        seller_sku=repair.seller_sku,
    )


def stale_ids(listing: AdapterListing) -> tuple[UUID, ...]:
    """The repairs no unit can apply any more: the seller's value at their field path changed
    since (retired `source_changed` by the publish job's terminal write)."""
    out: list[UUID] = []
    for repair in listing.repairs:
        units = [u for u in listing.units() if repair.seller_sku in (None, u.seller_sku)]
        if not any(
            digest(source_value(listing, u, repair.field_path)) == repair.source_digest
            for u in units
        ):
            out.append(repair.id)
    return tuple(out)


def units_as_built(listing: AdapterListing) -> list[tuple[AdapterItem, dict[str, Any]]]:
    """Each unit and the attributes its payload carries, as built with no stored repair (what
    `propose` reads)."""
    import dataclasses

    from app.adapters.amazon.mapping import build

    plain = dataclasses.replace(listing, repairs=())
    mapped, _errors = build(plain, schema=False, require_identifier=False)
    return [(m.unit, dict(m.payload["attributes"])) for m in mapped]


def attributes_for(listing: AdapterListing, field_path: str) -> list[str]:
    """The marketplace attributes the canonical `field_path` feeds (the issue's way back)."""
    from app.adapters.amazon.mapping import reverse_index

    template = listing.template.template if listing.template is not None else None
    return [n for n, feeds in reverse_index(template).items() if field_path in feeds]


def trail(listing: AdapterListing) -> tuple[tuple[str, str, str, str], ...]:
    """(seller SKU, field path, from digest hex, to digest hex) of every repair the payload
    applies: the `corrections_json` of an automatic resend (T41)."""
    from app.adapters.amazon.mapping import build

    mapped, _errors = build(listing, schema=False, require_identifier=False)
    return tuple(
        (m.unit.seller_sku, path, before.hex(), after.hex())
        for m in mapped
        for _id, path, before, after in m.repaired
    )
