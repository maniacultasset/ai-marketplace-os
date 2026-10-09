"""The Amazon category mappings for the two launch categories (S20 T5; PLAN D8; DATABASE.md:529-544).

Two new template rows, written by `seed_amazon_mappings` (`app/db/seed_ops.py`) through the S13
template loader, so a rerun is a no-op and content is never edited in place (0004's template
guard keeps it immutable; only `is_current` moves):

- Amazon `ethnic_wear_kurta_sets` **version 2**: S9's version 1 content plus the Amazon
  `product_type`, each spec's `marketplace_field` and `value_map` (adapter-only columns,
  invariant 4) and the three neutral axis sets as `variation_themes`. Promoted over S9's v1.
- Amazon `tshirts` **version 1**, current.

S9's v1 rows (`seed_ops._template_rows`) are not touched. `home_decor_lamps` stays unmapped.

Every Amazon name here (`product_type`, attribute names, enum spellings) is believed from the
public SP-API product type definitions, not yet confirmed against a sandbox or a PTD fetch: DW1
and DW2 confirm them, and `templates refresh` (T53) replaces them. The `required_attributes`
match the seeded RuleSets (`rules.seed.json`: `color`, `fabric_type`), so the adapter never
requires what the Rules Engine does not (invariant 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from mkos_schemas.jobs import CategoryTemplate

SOURCE: Final = "manual"

COLORS: Final = (
    "Black",
    "Blue",
    "Green",
    "Grey",
    "Indigo",
    "Maroon",
    "Navy",
    "Pink",
    "Red",
    "White",
    "Yellow",
)
COLOR_MAP: Final = {
    "navy blue": "Navy",
    "dark blue": "Navy",
    "wine": "Maroon",
    "burgundy": "Maroon",
    "off white": "White",
    "gray": "Grey",
}
SIZES: Final = ("XS", "S", "M", "L", "XL", "XXL", "3XL")
SIZE_MAP: Final = {"small": "S", "medium": "M", "large": "L", "2xl": "XXL", "xxxl": "3XL"}
FABRICS: Final = ("Cotton", "Linen", "Organic Cotton", "Polyester", "Rayon", "Silk", "Viscose")
FABRIC_MAP: Final = {"organic cotton blend": "Organic Cotton", "poly": "Polyester"}
SLEEVES: Final = ("Long Sleeve", "Short Sleeve", "Sleeveless", "Three-Quarter Sleeve")
SLEEVE_MAP: Final = {
    "long": "Long Sleeve",
    "full": "Long Sleeve",
    "short": "Short Sleeve",
    "half": "Short Sleeve",
    "sleeveless": "Sleeveless",
    "three_quarter": "Three-Quarter Sleeve",
    "3/4": "Three-Quarter Sleeve",
}
FITS: Final = ("Regular", "Relaxed", "Slim")
NECKS: Final = ("Crew Neck", "Mandarin Collar", "Polo Collar", "Round Neck", "V-Neck")
NECK_MAP: Final = {
    "round": "Round Neck",
    "crew": "Crew Neck",
    "v_neck": "V-Neck",
    "v neck": "V-Neck",
}
THEMES: Final = [["size"], ["color"], ["size", "color"]]


def _enum(
    field: str, allowed: tuple[str, ...], mapping: dict[str, str], req: str
) -> dict[str, Any]:
    return {
        "requirement": req,
        "marketplace_field": field,
        "value_type": "enum",
        "allowed_values": list(allowed),
        "value_map": dict(mapping),
    }


def _text(field: str, req: str = "optional", max_length: int | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"requirement": req, "marketplace_field": field, "value_type": "text"}
    if max_length is not None:
        spec["max_length"] = max_length
    return spec


def _apparel_specs() -> dict[str, Any]:
    return {
        "color": _enum("color", COLORS, COLOR_MAP, "required"),
        "fabric_type": _enum("fabric_type", FABRICS, FABRIC_MAP, "required"),
        "size": _enum("size", SIZES, SIZE_MAP, "optional"),
        "sleeve_length": _enum("sleeve_length_description", SLEEVES, SLEEVE_MAP, "optional"),
        "fit": _enum("fit_type", FITS, {}, "optional"),
        "neckline": _enum("neck_style", NECKS, NECK_MAP, "optional"),
        "pattern": _text("pattern"),
        "country_of_origin": _text("country_of_origin"),
        "number_of_pieces": {
            "requirement": "optional",
            "marketplace_field": "number_of_items",
            "value_type": "number",
        },
    }


def _kurta() -> dict[str, Any]:
    specs = _apparel_specs()
    specs["kurta_type"] = _text("style")
    return {
        "marketplace": "amazon",
        "category": "ethnic_wear_kurta_sets",
        "external_category_id": None,
        "required_attributes": ["color", "fabric_type"],
        "optional_attributes": sorted(k for k in specs if k not in ("color", "fabric_type")),
        "specs": specs,
        "variation_themes": THEMES,
    }


def _tshirt() -> dict[str, Any]:
    specs = _apparel_specs()
    specs["product_weight"] = {
        "requirement": "optional",
        "marketplace_field": "item_weight",
        "value_type": "measurement",
        "unit": "grams",
    }
    return {
        "marketplace": "amazon",
        "category": "tshirts",
        "external_category_id": None,
        "required_attributes": ["color", "fabric_type"],
        "optional_attributes": sorted(k for k in specs if k not in ("color", "fabric_type")),
        "specs": specs,
        "variation_themes": THEMES,
    }


@dataclass(frozen=True)
class SeedTemplate:
    product_type: str
    version: int
    template: CategoryTemplate


KURTA_PRODUCT_TYPE: Final = "KURTA"
TSHIRT_PRODUCT_TYPE: Final = "SHIRT"


def seed_templates() -> list[SeedTemplate]:
    """The two Amazon mappings, as the S13 loader takes them (no database needed)."""
    return [
        SeedTemplate(KURTA_PRODUCT_TYPE, 2, CategoryTemplate.model_validate(_kurta())),
        SeedTemplate(TSHIRT_PRODUCT_TYPE, 1, CategoryTemplate.model_validate(_tshirt())),
    ]
