"""Amazon SP-API Listings Items constants (spec L5272; S20 T6; PLAN D1-D3; SI2 T7-T8).

Every wire name here is believed from the public SP-API documentation. They stay believed until
the production definitions are fetched (DW2-P): since DW2, `app.cli templates diff` checks them
against a stored Product Type Definition. They live only in this adapter (invariant 4): the
canonical listing has none.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Final

API_VERSION: Final = "2021-08-01"
ITEMS_PATH: Final = f"/listings/{API_VERSION}/items"
LANGUAGE_TAG: Final = "en_IN"
# SI2 D3: a sellable item (standalone, child) carries the seller's offer; a parent stays
# product-only. No stock or fulfilment data (D16: DL1).
REQUIREMENTS_SELLABLE: Final = "LISTING"
REQUIREMENTS_PARENT: Final = "LISTING_PRODUCT_ONLY"

# SI2 T7 (D4): the offer. Price and MRP are the seller's own values, sent with tax.
OFFER_ATTRIBUTE: Final = "purchasable_offer"
OUR_PRICE: Final = "our_price"
MRP_FIELD: Final = "maximum_retail_price"
CONDITION_ATTRIBUTE: Final = "condition_type"
CONDITION_NEW: Final = "new_new"
CURRENCY: Final = "INR"
MAX_MONEY: Final = Decimal("9999999999.99")
REQUIRED_FACTS: Final = ("mrp", "selling_price", "manufacturer", "country_of_origin")
# SI2 T8: the second Amazon field the one manufacturer fact feeds (name and address)
MANUFACTURER_CONTACT: Final = "manufacturer_contact_information"

# C6: canonical content and keyword paths -> Amazon attributes
CONTENT_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "content.title": "item_name",
        "content.bullets": "bullet_point",
        "content.description": "product_description",
    }
)
KEYWORD_ATTRIBUTE: Final = "generic_keyword"
KEYWORD_PATHS: Final = ("keywords.search_terms", "keywords.hidden_terms")
MAIN_IMAGE: Final = "main_product_image_locator"
OTHER_IMAGES: Final = tuple(f"other_product_image_locator_{i}" for i in range(1, 9))
MAX_IMAGES: Final = 1 + len(OTHER_IMAGES)

# C10: the neutral variation vocabulary (DATABASE.md:416, ADR-0007:20) -> axes -> Amazon theme
NEUTRAL_THEME_AXES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "size": frozenset({"size"}),
        "color": frozenset({"color"}),
        "size_color": frozenset({"size", "color"}),
    }
)
AMAZON_THEMES: Final[Mapping[frozenset[str], str]] = MappingProxyType(
    {
        frozenset({"size"}): "SIZE",
        frozenset({"color"}): "COLOR",
        frozenset({"size", "color"}): "SIZE/COLOR",
    }
)
THEME_ATTRIBUTE: Final = "variation_theme"
PARENTAGE_ATTRIBUTE: Final = "parentage_level"
RELATIONSHIP_ATTRIBUTE: Final = "child_parent_sku_relationship"
VARIATION_ATTRIBUTES: Final = (THEME_ATTRIBUTE, PARENTAGE_ATTRIBUTE, RELATIONSHIP_ATTRIBUTE)

# D3: the outbound limiter's default rates, calls per second per (account, operation). Believed
# from the published Listings Items quotas (5 req/s burst 10); 1/s is the safe start. DW1.
OPERATIONS: Final = ("put", "patch", "get")
RATES: Final[Mapping[str, int]] = MappingProxyType({"put": 1, "patch": 1, "get": 1})

# The issue severities and the summary states SP-API answers with.
ACCEPTED: Final = "ACCEPTED"
INVALID: Final = "INVALID"
BUYABLE: Final = "BUYABLE"
DISCOVERABLE: Final = "DISCOVERABLE"

# DW2: the Product Type Definitions API (2020-09-01). Only `getDefinitionsProductType` is used.
PTD_API_VERSION: Final = "2020-09-01"
PTD_PATH: Final = f"/definitions/{PTD_API_VERSION}/productTypes"
PTD_LOCALE: Final = "en_IN"
PTD_REQUIREMENTS: Final = REQUIREMENTS_PARENT  # decision 2: store and validate what is sent
PTD_REQUIREMENTS_VALUES: Final = ("LISTING", "LISTING_PRODUCT_ONLY", "LISTING_OFFER_ONLY")
PTD_OPERATIONS: Final = ("ptd_get",)
# Believed: Amazon publishes 5 req/s; 1/s is the safe start for a one-shot staff command.
PTD_RATES: Final[Mapping[str, int]] = MappingProxyType({"ptd_get": 1})
PRODUCT_TYPE_PATTERN: Final = r"^[A-Z0-9_]{1,64}$"
SCHEMA_MAX_BODY: Final = 16 * 1024 * 1024
MAX_SCHEMA_ERRORS: Final = 50
