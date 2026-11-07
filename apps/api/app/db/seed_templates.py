"""The Amazon category mappings for the two launch categories (S20 T5; PLAN D8; DATABASE.md:529-544).

Two new template rows, written by `seed_amazon_mappings` (`app/db/seed_ops.py`) through the S13
template loader, so a rerun is a no-op and content is never edited in place (0004's template
guard keeps it immutable; only `is_current` moves):

- Amazon `ethnic_wear_kurta_sets` **version 3** (SI2): version 2 (S9's v1 content plus the
  Amazon `product_type`, each spec's `marketplace_field` and `value_map`, adapter-only columns,
  invariant 4, and the three neutral axis sets as `variation_themes`) with wider colour and
  fabric lists and value maps, `fabric_type` sent as `material`, and the seller-fact specs
  `fiber_composition` (Amazon `fabric_type`), `manufacturer` and an enum `country_of_origin`
  (ISO codes). Promoted over version 2.
- Amazon `tshirts` **version 2** (SI2), the same changes over version 1.

S9's v1 rows (`seed_ops._template_rows`) are not touched. A2 (ai-deps PLAN) adds the
`home_decor_lamps` mappings for both marketplaces at the end of this module.

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

# SI2: the seller's own colours and fabrics (gap doc :15); every map target is an allowed value
_EXTRA_COLORS: Final = (
    "Beige",
    "Brown",
    "Charcoal",
    "Coral",
    "Cream",
    "Gold",
    "Lavender",
    "Mint",
    "Multicolor",
    "Mustard",
    "Off White",
    "Olive",
    "Orange",
    "Peach",
    "Purple",
    "Rust",
    "Silver",
    "Teal",
)
_COLOR_SYNONYMS: Final = {
    "multicolour": "Multicolor",
    "multi": "Multicolor",
    "off-white": "Off White",
    "off white": "Off White",
    "mustard yellow": "Mustard",
    "peach puff": "Peach",
}
COLORS: Final = tuple(
    sorted(
        (
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
            *_EXTRA_COLORS,
        )
    )
)
COLOR_MAP: Final = {
    "navy blue": "Navy",
    "dark blue": "Navy",
    "wine": "Maroon",
    "burgundy": "Maroon",
    "gray": "Grey",
    **_COLOR_SYNONYMS,
}
SIZES: Final = ("XS", "S", "M", "L", "XL", "XXL", "3XL")
SIZE_MAP: Final = {"small": "S", "medium": "M", "large": "L", "2xl": "XXL", "xxxl": "3XL"}
_EXTRA_FABRICS: Final = (
    "Chanderi",
    "Chiffon",
    "Cotton Blend",
    "Cotton Lycra",
    "Crepe",
    "Denim",
    "Georgette",
    "Khadi",
    "Modal",
    "Muslin",
    "Net",
    "Satin",
    "Velvet",
    "Wool",
)
_FABRIC_SYNONYMS: Final = {
    "cotton spandex": "Cotton Lycra",
    "lycra cotton": "Cotton Lycra",
    "poly cotton": "Cotton Blend",
}
FABRICS: Final = tuple(
    sorted(
        (
            "Cotton",
            "Linen",
            "Organic Cotton",
            "Polyester",
            "Rayon",
            "Silk",
            "Viscose",
            *_EXTRA_FABRICS,
        )
    )
)
FABRIC_MAP: Final = {
    "organic cotton blend": "Organic Cotton",
    "poly": "Polyester",
    **_FABRIC_SYNONYMS,
}
# SI2 (AC6): the country of origin. Amazon takes ISO 3166-1 alpha-2 codes (believed; DW1/DW2),
# Flipkart the English name. Each map takes the common names and spellings sellers write.
COUNTRIES: Final = {
    "BD": "Bangladesh",
    "CN": "China",
    "GB": "United Kingdom",
    "ID": "Indonesia",
    "IN": "India",
    "IT": "Italy",
    "LK": "Sri Lanka",
    "NP": "Nepal",
    "PK": "Pakistan",
    "TH": "Thailand",
    "TR": "Turkey",
    "US": "United States",
    "VN": "Vietnam",
}
_COUNTRY_SYNONYMS: Final = {
    "bharat": "IN",
    "hindustan": "IN",
    "prc": "CN",
    "people's republic of china": "CN",
    "uk": "GB",
    "great britain": "GB",
    "britain": "GB",
    "england": "GB",
    "usa": "US",
    "united states of america": "US",
    "america": "US",
    "türkiye": "TR",
    "turkiye": "TR",
    "viet nam": "VN",
}
COUNTRY_CODES: Final = tuple(sorted(COUNTRIES))
COUNTRY_TO_CODE: Final = {
    **{name.casefold(): code for code, name in COUNTRIES.items()},
    **_COUNTRY_SYNONYMS,
}
COUNTRY_NAMES: Final = tuple(sorted(COUNTRIES.values()))
CODE_TO_NAME: Final = {
    **{code.casefold(): name for code, name in COUNTRIES.items()},
    **{k: COUNTRIES[v] for k, v in _COUNTRY_SYNONYMS.items()},
}
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
        "fabric_type": _enum("material", FABRICS, FABRIC_MAP, "required"),
        "size": _enum("size", SIZES, SIZE_MAP, "optional"),
        "sleeve_length": _enum("sleeve_length_description", SLEEVES, SLEEVE_MAP, "optional"),
        "fit": _enum("fit_type", FITS, {}, "optional"),
        "neckline": _enum("neck_style", NECKS, NECK_MAP, "optional"),
        "pattern": _text("pattern"),
        "country_of_origin": _enum("country_of_origin", COUNTRY_CODES, COUNTRY_TO_CODE, "optional"),
        "number_of_pieces": {
            "requirement": "optional",
            "marketplace_field": "number_of_items",
            "value_type": "number",
        },
        # SI2: seller facts the adapter sends at `facts.<key>` (never AI attributes)
        "fiber_composition": _text("fabric_type"),
        "manufacturer": _text("manufacturer"),
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
    """The three Amazon mappings, as the S13 loader takes them (no database needed)."""
    return [
        SeedTemplate(KURTA_PRODUCT_TYPE, 3, CategoryTemplate.model_validate(_kurta())),
        SeedTemplate(TSHIRT_PRODUCT_TYPE, 2, CategoryTemplate.model_validate(_tshirt())),
        SeedTemplate(LAMP_PRODUCT_TYPE, 2, CategoryTemplate.model_validate(_amazon_lamp())),
    ]


# ---- S22 T5/T15 (PLAN D4, D7; C7): the Flipkart mappings for the two launch categories ----
#
# Flipkart kurta **version 3** and `tshirts` **version 2** (SI2): version 2 / 1 (S9's v1 content
# plus the Flipkart vertical, each spec's `marketplace_field` and `value_map`) with the same wider
# colour and fabric lists as Amazon, in Flipkart spelling, and the seller-fact specs
# `fiber_composition` (`Fabric Composition`), `manufacturer` (`Manufacturer Details`) and an enum
# `country_of_origin` (English names). Every Flipkart name (vertical, attribute names, enum
# spellings) is believed (spec L10635: canonical `color="navy blue"` -> Flipkart `Colour="Navy
# Blue"`), not confirmed: DW1 and DW2 confirm them. The lamp mapping (A2) is at the end of this
# module. Price and MRP travel as the adapter's own `price` block (SI2 T10), never as a template
# spec; no stock, HSN, package or fulfilment attribute.

FK_COLORS: Final = tuple(
    sorted(
        (
            "Black",
            "Blue",
            "Green",
            "Grey",
            "Indigo",
            "Maroon",
            "Navy Blue",
            "Pink",
            "Red",
            "White",
            "Yellow",
            *_EXTRA_COLORS,
        )
    )
)
FK_COLOR_MAP: Final = {
    "navy blue": "Navy Blue",
    "navy": "Navy Blue",
    "dark blue": "Navy Blue",
    "wine": "Maroon",
    "burgundy": "Maroon",
    "gray": "Grey",
    **_COLOR_SYNONYMS,
}
FK_FABRICS: Final = tuple(
    sorted(
        {
            "Cotton",
            "Cotton Blend",
            "Linen",
            "Polyester",
            "Rayon",
            "Silk",
            "Viscose",
            *_EXTRA_FABRICS,
        }
    )
)
FK_FABRIC_MAP: Final = {
    "organic cotton": "Cotton",
    "organic cotton blend": "Cotton Blend",
    "poly": "Polyester",
    **_FABRIC_SYNONYMS,
}
FK_SLEEVES: Final = ("3/4 Sleeve", "Full Sleeve", "Half Sleeve", "Sleeveless")
FK_SLEEVE_MAP: Final = {
    "long": "Full Sleeve",
    "full": "Full Sleeve",
    "short": "Half Sleeve",
    "half": "Half Sleeve",
    "sleeveless": "Sleeveless",
    "three_quarter": "3/4 Sleeve",
}
FK_NECKS: Final = ("Collared Neck", "Mandarin Collar", "Round Neck", "V-Neck")
FK_NECK_MAP: Final = {
    "round": "Round Neck",
    "crew": "Round Neck",
    "v_neck": "V-Neck",
    "v neck": "V-Neck",
    "polo": "Collared Neck",
}


def _flipkart_apparel_specs() -> dict[str, Any]:
    return {
        "color": _enum("Colour", FK_COLORS, FK_COLOR_MAP, "required"),
        "fabric_type": _enum("Fabric", FK_FABRICS, FK_FABRIC_MAP, "required"),
        "size": _enum("Size", SIZES, SIZE_MAP, "optional"),
        "sleeve_length": _enum("Sleeve", FK_SLEEVES, FK_SLEEVE_MAP, "optional"),
        "fit": _enum("Fit", FITS, {}, "optional"),
        "neckline": _enum("Neck", FK_NECKS, FK_NECK_MAP, "optional"),
        "pattern": _text("Pattern"),
        "country_of_origin": _enum("Country of Origin", COUNTRY_NAMES, CODE_TO_NAME, "optional"),
        "number_of_pieces": {
            "requirement": "optional",
            "marketplace_field": "Number of Contents in Sales Package",
            "value_type": "number",
        },
        # SI2: seller facts the adapter sends at `facts.<key>` (never AI attributes)
        "fiber_composition": _text("Fabric Composition"),
        "manufacturer": _text("Manufacturer Details"),
    }


def _flipkart(category: str, extra: dict[str, Any]) -> dict[str, Any]:
    specs = {**_flipkart_apparel_specs(), **extra}
    return {
        "marketplace": "flipkart",
        "category": category,
        "external_category_id": None,
        "required_attributes": ["color", "fabric_type"],
        "optional_attributes": sorted(k for k in specs if k not in ("color", "fabric_type")),
        "specs": specs,
        "variation_themes": THEMES,
    }


FLIPKART_KURTA_VERTICAL: Final = "kurta"
FLIPKART_TSHIRT_VERTICAL: Final = "t_shirt"


def flipkart_seed_templates() -> list[SeedTemplate]:
    """The three Flipkart mappings, as the S13 loader takes them (no database needed)."""
    kurta = _flipkart("ethnic_wear_kurta_sets", {"kurta_type": _text("Type")})
    tshirt = _flipkart("tshirts", {})
    lamp = _flipkart_lamp()
    return [
        SeedTemplate(FLIPKART_KURTA_VERTICAL, 3, CategoryTemplate.model_validate(kurta)),
        SeedTemplate(FLIPKART_TSHIRT_VERTICAL, 2, CategoryTemplate.model_validate(tshirt)),
        SeedTemplate(FLIPKART_LAMP_VERTICAL, 2, CategoryTemplate.model_validate(lamp)),
    ]


# ---- A2 (ai-deps PLAN; C1, C20): the home_decor_lamps mappings ----
#
# Amazon `LAMP` and Flipkart `table_lamp`, both **version 2**: promoted over S9's empty v1 rows
# (left as they are), or over nothing on a database without them (prod). `required_attributes`
# are `color` and `product_dimensions`, the attributes the seeded lamp RuleSets require
# (invariant 2). The lamp keys are canonical dictionary keys; the lamp material is
# `lamp_material`, because `material` is already `fabric_type`'s input alias (C1). A lamp family
# varies by colour only. The product dimensions travel as the seller's text (for example
# "30 x 15 x 15 cm"). Every marketplace name here (product type, vertical, attribute names, enum
# spellings) is believed, not confirmed: DW1 and DW2 confirm them, as for the apparel mappings.

LAMP_PRODUCT_TYPE: Final = "LAMP"
FLIPKART_LAMP_VERTICAL: Final = "table_lamp"
LAMP_THEMES: Final = [["color"]]
LAMP_REQUIRED: Final = ("color", "product_dimensions")
LAMP_COLORS: Final = (
    "Beige",
    "Black",
    "Blue",
    "Brass",
    "Bronze",
    "Brown",
    "Copper",
    "Gold",
    "Green",
    "Grey",
    "Red",
    "Silver",
    "White",
    "Yellow",
)
LAMP_COLOR_MAP: Final = {
    "golden": "Gold",
    "antique brass": "Brass",
    "gray": "Grey",
    "off white": "White",
    "cream": "Beige",
}
FK_LAMP_COLORS: Final = (*LAMP_COLORS, "Navy Blue")
FK_LAMP_COLOR_MAP: Final = {**LAMP_COLOR_MAP, "navy blue": "Navy Blue", "navy": "Navy Blue"}


def _lamp(marketplace: str, specs: dict[str, Any]) -> dict[str, Any]:
    return {
        "marketplace": marketplace,
        "category": "home_decor_lamps",
        "external_category_id": None,
        "required_attributes": list(LAMP_REQUIRED),
        "optional_attributes": sorted(k for k in specs if k not in LAMP_REQUIRED),
        "specs": specs,
        "variation_themes": LAMP_THEMES,
    }


# product_dimensions goes out as one text value, a stopgap: the dictionary declares it a measure in
# cm, and Amazon's item_dimensions is likely structured (length, width, height, each with a unit).
# DW1/DW2 confirm the shape; a structured one needs a multi-part measurement in the adapter.
def _amazon_lamp() -> dict[str, Any]:
    return _lamp(
        "amazon",
        {
            "color": _enum("color", LAMP_COLORS, LAMP_COLOR_MAP, "required"),
            "product_dimensions": _text("item_dimensions", "required"),
            "lamp_type": _text("lamp_type"),
            "lamp_material": _text("material"),
            "shade_material": _text("shade_material"),
            "light_source_type": _text("light_source_type"),
            "country_of_origin": _text("country_of_origin"),
        },
    )


def _flipkart_lamp() -> dict[str, Any]:
    return _lamp(
        "flipkart",
        {
            "color": _enum("Colour", FK_LAMP_COLORS, FK_LAMP_COLOR_MAP, "required"),
            "product_dimensions": _text("Dimensions", "required"),
            "lamp_type": _text("Type"),
            "lamp_material": _text("Material"),
            "shade_material": _text("Shade Material"),
            "light_source_type": _text("Bulb Type"),
            "country_of_origin": _text("Country of Origin"),
        },
    )
