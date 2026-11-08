"""SI12 B5: a suggested HSN heading from the product's category (pure, nothing stored).

A reviewed map from each seeded category to its 4-digit heading in the GST HSN schedule (PLAN D6;
for the client to review; the seller always confirms by saving it). Only the 4-digit heading: the
GST rate split by price is not ours to guess. Any other category, or a product that already has
an HSN code, gets no suggestion. Generation, rules and adapters never read this.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

__all__ = ["HSN_BY_CATEGORY", "HsnSuggestion", "suggest_hsn"]

HSN_BY_CATEGORY: Final = MappingProxyType(
    {
        # 6109: T-shirts, singlets and other vests, knitted or crocheted
        "tshirts": "6109",
        # 6204: women's or girls' suits, ensembles, dresses and the like, not knitted
        "ethnic_wear_kurta_sets": "6204",
        # 9405: luminaires and lighting fittings, including lamps
        "home_decor_lamps": "9405",
    }
)


@dataclass(frozen=True, slots=True)
class HsnSuggestion:
    value: str
    basis: Literal["category"] = "category"


def suggest_hsn(category: str | None, current: str | None) -> HsnSuggestion | None:
    """The heading for `category`, only while the effective HSN code is empty."""
    if current is not None or not category:
        return None
    value = HSN_BY_CATEGORY.get(category)
    return None if value is None else HsnSuggestion(value=value)
