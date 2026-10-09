"""Amazon SP-API Listings Items constants (spec L5272; S20 T6; PLAN D1-D3).

Every wire name here is believed from the public SP-API documentation and has not been checked
against a sandbox or a product type definition yet: DW1 (sandbox) and DW2 (PTD templates)
confirm them. They live only in this adapter (invariant 4): the canonical listing has none.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

API_VERSION: Final = "2021-08-01"
ITEMS_PATH: Final = f"/listings/{API_VERSION}/items"
LANGUAGE_TAG: Final = "en_IN"
REQUIREMENTS: Final = "LISTING_PRODUCT_ONLY"  # D2: no offer data (price, stock) in v1

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
