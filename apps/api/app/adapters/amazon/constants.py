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
REQUIRED_FACTS: Final = ("mrp", "selling_price", "manufacturer", "country_of_origin", "brand_name")
# SI12 B7 (PLAN D7, CP-D1): the other dictionary keys Amazon needs before it accepts a listing,
# shown to the seller as "Needed" (key None: every category). `identifier`: a barcode or the GTIN
# exemption (SI5, `services/product_identifier.py`); `gender`: the Kurta set and T-shirt product
# types send target_gender, department and age_range_description from it (SI8).
EXTRA_NEEDS: Final[Mapping[str | None, tuple[str, ...]]] = MappingProxyType(
    {
        None: ("identifier",),
        "tshirts": ("gender",),
        "ethnic_wear_kurta_sets": ("gender",),
    }
)
# SI2 T8: the second Amazon field the one manufacturer fact feeds (name and address)
MANUFACTURER_CONTACT: Final = "manufacturer_contact_information"
# SI5 B2 (PLAN §2.7): a sellable item's barcode, or the seller's GTIN exemption (never both,
# never on a parent). Our identifier type -> Amazon's `type` spelling.
EXTERNAL_ID_ATTRIBUTE: Final = "externally_assigned_product_identifier"
EXEMPTION_ATTRIBUTE: Final = "supplier_declared_has_product_identifier_exemption"
IDENTIFIER_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {"ean": "ean", "upc": "upc", "gtin": "gtin"}
)

# SI8 T3: the identity attributes. Believed (names, entry shapes and the audience values) until
# the real LISTING definitions are stored and `templates diff` checks them (SI8 DL-A).
BRAND_ATTRIBUTE: Final = "brand"
MODEL_NUMBER: Final = "model_number"
PART_NUMBER: Final = "part_number"
TARGET_GENDER: Final = "target_gender"
DEPARTMENT: Final = "department"
AGE_RANGE: Final = "age_range_description"
AUDIENCE_ATTRIBUTES: Final = (TARGET_GENDER, DEPARTMENT, AGE_RANGE)
AUDIENCE: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        gender: MappingProxyType(dict(zip(AUDIENCE_ATTRIBUTES, values, strict=True)))
        for gender, values in {
            "women": ("female", "Women", "Adult"),
            "men": ("male", "Men", "Adult"),
            "unisex": ("unisex", "Unisex", "Adult"),
            "girls": ("female", "Girls", "Kid"),
            "boys": ("male", "Boys", "Kid"),
            "baby": ("unisex", "Baby", "Baby"),
        }.items()
    }
)
"""`products.gender` -> the three Amazon audience attributes (SI8 PLAN §2.2; believed, DL-A)."""
AUDIENCE_PRODUCT_TYPES: Final = frozenset({"KURTA", "SHIRT"})
"""The apparel product types that send (and need) the audience."""
TEXT_IDENTITY: Final = frozenset({BRAND_ATTRIBUTE, DEPARTMENT, AGE_RANGE})
"""The identity attributes whose entries carry `language_tag`."""
ITEM_NAME_MAX: Final = 200
"""The child title limit when no stored definition gives `item_name.maxLength` (SI8 T5)."""

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
# SI13 (CP-D11): the Amazon attributes a theme axis may be asked under (a child missing one is
# filled from its own variant value, `fill_variant_axis`)
AXIS_ATTRIBUTES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {"size": ("size", "size_name"), "color": ("color", "color_name")}
)
THEME_ATTRIBUTE: Final = "variation_theme"
PARENTAGE_ATTRIBUTE: Final = "parentage_level"
RELATIONSHIP_ATTRIBUTE: Final = "child_parent_sku_relationship"
VARIATION_ATTRIBUTES: Final = (THEME_ATTRIBUTE, PARENTAGE_ATTRIBUTE, RELATIONSHIP_ATTRIBUTE)

# D3: the outbound limiter's default rates, calls per second per (account, operation). Believed
# from the published Listings Items quotas (5 req/s burst 10); 1/s is the safe start. DW1.
# SI11: `put_preview` (VALIDATION_PREVIEW) has its own bucket: Amazon publishes a separate, larger
# preview quota (20 req/s per app, F:60); 1/s per account is the safe start.
# SI13 (CP-D15): `catalog_search` (Catalog Items 2022-04-01 searchCatalogItems; Amazon publishes
# 2 req/s) and `restrictions_get` (Listings Restrictions 2021-08-01 getListingsRestrictions; Amazon
# publishes 5 req/s) start at 1/s per account, the read-only pre-check inside the dry-run job.
OPERATIONS: Final = ("put", "patch", "get", "put_preview", "catalog_search", "restrictions_get")
RATES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "put": 1,
        "patch": 1,
        "get": 1,
        "put_preview": 1,
        "catalog_search": 1,
        "restrictions_get": 1,
    }
)
CATALOG_API_VERSION: Final = "2022-04-01"
CATALOG_PATH: Final = f"/catalog/{CATALOG_API_VERSION}/items"
RESTRICTIONS_PATH: Final = f"/listings/{API_VERSION}/restrictions"
CATALOG_MAX_IDENTIFIERS: Final = 20  # searchCatalogItems takes up to 20 identifiers per call
CATALOG_INCLUDED: Final = "summaries,identifiers"
# searchCatalogItems pages: pageSize default 10, max 20; `pagination.nextToken` -> `pageToken`
# https://developer-docs.amazon.com/sp-api/reference/searchcatalogitems
CATALOG_PAGE_SIZE: Final = 20
CATALOG_MAX_PAGES: Final = 5  # one EAN can match several ASINs; past this the search is partial
RESTRICTION_CONDITION: Final = "new_new"
# The Amazon connection check (`app.workers.amazon_check`): Sellers API v1
# getMarketplaceParticipations, a read with no query and no seller in the path. Believed from the
# public docs (Amazon publishes 0.016 req/s, burst 15).
# https://developer-docs.amazon.com/sp-api/reference/getmarketplaceparticipations
PARTICIPATIONS_PATH: Final = "/sellers/v1/marketplaceParticipations"

# The issue severities and the summary states SP-API answers with.
ACCEPTED: Final = "ACCEPTED"
INVALID: Final = "INVALID"
BUYABLE: Final = "BUYABLE"
DISCOVERABLE: Final = "DISCOVERABLE"

# DW2: the Product Type Definitions API (2020-09-01). Only `getDefinitionsProductType` is used.
PTD_API_VERSION: Final = "2020-09-01"
PTD_PATH: Final = f"/definitions/{PTD_API_VERSION}/productTypes"
PTD_LOCALE: Final = "en_IN"
# decision 2: store and validate what is sent. SI2 D3: two definitions per product type, the
# `LISTING` one for sellable items (the `templates refresh|diff` default) and the PRODUCT_ONLY
# one for a family's parent.
PTD_REQUIREMENTS: Final = REQUIREMENTS_SELLABLE
PTD_PARENT_REQUIREMENTS: Final = REQUIREMENTS_PARENT
PTD_REQUIREMENTS_VALUES: Final = ("LISTING", "LISTING_PRODUCT_ONLY", "LISTING_OFFER_ONLY")
PTD_OPERATIONS: Final = ("ptd_get",)
# Believed: Amazon publishes 5 req/s; 1/s is the safe start for a one-shot staff command.
PTD_RATES: Final[Mapping[str, int]] = MappingProxyType({"ptd_get": 1})
PRODUCT_TYPE_PATTERN: Final = r"^[A-Z0-9_]{1,64}$"
SCHEMA_MAX_BODY: Final = 16 * 1024 * 1024
MAX_SCHEMA_ERRORS: Final = 50
