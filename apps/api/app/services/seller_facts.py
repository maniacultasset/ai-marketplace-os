"""Seller facts and the seller-value overlay (SI2 T3; PLAN §2.1-2.2, D5, D15; AC7).

Pure: no I/O and no marketplace names (invariant 4). The Golden Rule (spec L763-765): a factual
attribute is never the AI's guess. Two paths carry the seller's facts to an adapter:

- `seller_facts(facts)`: the facts that are not listing attributes (money, country, the
  manufacturer, the fibre composition, and since SI5 the barcode or its exemption) as one
  `SellerFacts` per item, reported at `facts.<key>`.
- `seller_overlay(listing, facts, template)`: the canonical listing with every factual attribute
  set from the seller's own value, or dropped when the seller gave none. The keys `SellerFacts`
  carries (`FACT_ONLY_KEYS`) leave `attributes`, so no marketplace field has two sources. The one
  column fact a template reads as an attribute (`product_weight`, the tshirts `item_weight`) is
  copied from `ProductFacts.product_weight_g`. A colour with no seller value keeps the AI's only
  when the dictionary lets `ai` supply it and the listing's `confidence_score` reaches the
  dictionary's `min_confidence` (CP-D8). A style's variation axes are left alone: each child
  sends its own seller value.

The seller value of an attribute is the stored Fact's `value` (the seller's spelling, SI1 S-f),
else its `normalized` form, as the product API reads it (`services/products.py:207-212`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from importlib import resources
from typing import Any, Final

from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.listing import AttributeValue, CanonicalListing

from app.adapters.base import SellerFacts, money
from app.core.resolved_facts import ProductFacts, ResolvedFact

__all__ = [
    "COLUMN_ATTRIBUTES",
    "FACTUAL_KEYS",
    "FACT_ONLY_KEYS",
    "seller_facts",
    "seller_overlay",
]

_DICTIONARY: Final = ("mkos_schemas", "data/attribute_dictionary.json")


def _entries() -> dict[str, dict[str, Any]]:
    package, path = _DICTIONARY
    raw = json.loads(resources.files(package).joinpath(path).read_text(encoding="utf-8"))
    return {entry["key"]: entry for entry in raw["attributes"]}


_ENTRIES: Final = _entries()
FACTUAL_KEYS: Final[frozenset[str]] = frozenset(
    {k for k, e in _ENTRIES.items() if e.get("is_critical")} | {"color", "size"}
)
COLUMN_ATTRIBUTES: Final[Mapping[str, str]] = {"product_weight": "product_weight_g"}
FACT_ONLY_KEYS: Final[frozenset[str]] = frozenset(
    (
        {k for k in FACTUAL_KEYS if _ENTRIES.get(k, {}).get("storage") == "column"}
        | {"fiber_composition"}
    )
    - set(COLUMN_ATTRIBUTES)
)
_AXES: Final[Mapping[str, frozenset[str]]] = {
    "size": frozenset({"size"}),
    "color": frozenset({"color"}),
    "size_color": frozenset({"size", "color"}),
}


def _money(value: str | None) -> Decimal | None:
    try:
        return money(value)
    except ValueError:
        return None  # unreachable from Numeric(12, 2); the adapter reports MISSING_ATTRIBUTE


def _identifier(facts: ProductFacts) -> tuple[str | None, str | None, bool]:
    """SI5 B1: (type, value, exempt) of a sellable product (never a style). A barcode only with
    `provided` and a non-blank value; the exemption only with `exempt`."""
    found = facts.identifier
    if facts.level == "style":
        return None, None, False
    if found.status == "provided" and found.type and (found.value or "").strip():
        return found.type, found.value, False
    return None, None, found.status == "exempt"


def seller_facts(facts: ProductFacts) -> SellerFacts:
    commercial = facts.commercial
    party = facts.compliance.manufacturer
    name = None if party is None or not (party.legal_name or "").strip() else party.legal_name
    composition = facts.attributes.get("fiber_composition")
    text: str | None = None
    parts: object | None = None
    if composition is not None:
        if isinstance(composition.value, str):
            text = composition.value
            parts = composition.normalized
        elif isinstance(composition.value, list) and composition.normalized is None:
            parts = composition.value  # a legacy Fact that stored the list as its value
        else:
            parts = composition.normalized
    id_type, id_value, exempt = _identifier(facts)
    return SellerFacts(
        mrp=_money(commercial.mrp),
        selling_price=_money(commercial.selling_price),
        currency=commercial.currency,
        country_of_origin=facts.compliance.country_of_origin,
        manufacturer_name=name,
        manufacturer_address=None if name is None or party is None else party.address,
        fiber_composition_text=text,
        fiber_composition_parts=parts,
        brand_name=facts.identity.brand_name,
        gender=facts.identity.gender,
        style_number=facts.identity.style_number,
        identifier_type=id_type,
        identifier_value=id_value,
        identifier_exempt=exempt,
    )


def _valid(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool | int | float):
        return True
    if isinstance(value, list):
        return bool(value) and all(isinstance(v, str) and v.strip() for v in value)
    return False


def _seller_value(fact: ResolvedFact | None) -> AttributeValue | None:
    if fact is None:
        return None
    for candidate in (fact.value, fact.normalized):
        if _valid(candidate):
            return candidate  # type: ignore[no-any-return]
    return None


def _axes(facts: ProductFacts) -> frozenset[str]:
    if facts.level != "style" or facts.family is None or facts.family.theme is None:
        return frozenset()
    return _AXES.get(facts.family.theme, frozenset())


def _ai_colour_kept(listing: CanonicalListing) -> bool:
    entry = _ENTRIES.get("color", {})
    if "ai" not in (entry.get("allowed_sources") or ()):
        return False
    floor = entry.get("min_confidence")
    score = listing.confidence_score
    if score is None or floor is None:
        return False
    return Decimal(str(score)) >= Decimal(str(floor))


def seller_overlay(
    listing: CanonicalListing, facts: ProductFacts, template: CategoryTemplate | None
) -> CanonicalListing:
    """`listing` with every factual attribute from the seller (module docstring). Idempotent."""
    attributes: dict[str, AttributeValue] = {
        k: v for k, v in listing.attributes.items() if k not in FACT_ONLY_KEYS
    }
    axes = _axes(facts)
    named = set(template.specs) if template is not None else set()
    wanted = (set(attributes) | named) & (FACTUAL_KEYS - FACT_ONLY_KEYS - axes)
    sources = dict(listing.attribute_sources)
    for key in sorted(wanted):
        if key in COLUMN_ATTRIBUTES:
            value: Any = getattr(facts, COLUMN_ATTRIBUTES[key])
            seller = value if _valid(value) else None
        else:
            seller = _seller_value(facts.attributes.get(key))
        if seller is not None:
            attributes[key] = seller
            sources[key] = COLUMN_ATTRIBUTES.get(key, key)
        elif key == "color" and key in attributes and _ai_colour_kept(listing):
            continue
        else:
            attributes.pop(key, None)
    sources = {k: v for k, v in sources.items() if k in attributes}
    return listing.model_copy(update={"attributes": attributes, "attribute_sources": sources})
