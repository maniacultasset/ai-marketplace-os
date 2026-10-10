"""Flipkart Seller API listing constants (spec L10573-10575, L10635; S22 T5; PLAN D3, D4; C6, C7).

Every wire name here is believed, DW1: Flipkart has confirmed no sandbox or test account (T78)
and its Listing Management API shapes are not in the repo (spec L2276 only links them). DW1
(credentials + one supervised publish) and DW2 (vertical schemas from the API) confirm or
replace them. They live only in this adapter and in the Flipkart template data (invariant 4):
the canonical listing has none.

v1 sends content only: no price, MRP, stock, HSN, package or fulfilment field ever (user
decision; S20 D2). `FORBIDDEN_ATTRIBUTES` names them so a test can prove it.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

# C6: paths under FLIPKART_API_BASE (believed, DW1)
CREATE_PATH: Final = "/listings/v3"  # POST, a batch of listings
UPDATE_PATH: Final = "/listings/v3/update"  # POST, a batch, named attributes only
GET_PATH: Final = "/listings/v3/{sku_ids}"  # GET, comma-joined seller SKUs
CHUNK: Final = 10  # listings per create/update/get request (believed, DW1)

# D3: the outbound limiter's default rates, calls per second per (account, operation); 1/s is
# the safe start until Flipkart publishes its quotas (DW1).
OPERATIONS: Final = ("create", "update", "get")
RATES: Final[Mapping[str, int]] = MappingProxyType({"create": 1, "update": 1, "get": 1})

# C7: canonical content and keyword paths -> Flipkart attributes (believed, DW1)
CONTENT_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "content.title": "title",
        "content.bullets": "key_features",
        "content.description": "description",
    }
)
KEYWORD_ATTRIBUTE: Final = "keywords"
KEYWORD_PATHS: Final = ("keywords.search_terms", "keywords.hidden_terms")
IMAGE_ATTRIBUTE: Final = "images"
MAX_IMAGES: Final = 10

# C7: the family grouping attributes (DATABASE.md:438, :471): no parent row; every child names
# its style and its colour group.
STYLE_ATTRIBUTE: Final = "style_code"
COLOUR_GROUP_ATTRIBUTE: Final = "colour_group"
GROUP_ATTRIBUTES: Final = (STYLE_ATTRIBUTE, COLOUR_GROUP_ATTRIBUTE)

# The neutral variation vocabulary (DATABASE.md:416, ADR-0007:20) -> its axes.
NEUTRAL_THEME_AXES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "size": frozenset({"size"}),
        "color": frozenset({"color"}),
        "size_color": frozenset({"size", "color"}),
    }
)

# Response vocabulary (believed, DW1).
SUCCESS: Final = "SUCCESS"
FAILURE: Final = "FAILURE"
ACTIVE: Final = "ACTIVE"
WARNING: Final = "WARNING"

# Never sent in v1 (user decision: content only). Any payload key here is a bug.
FORBIDDEN_ATTRIBUTES: Final = frozenset(
    {
        "mrp",
        "selling_price",
        "price",
        "stock",
        "stock_count",
        "inventory",
        "hsn",
        "tax_code",
        "package_length",
        "package_breadth",
        "package_height",
        "package_weight",
        "fulfilled_by",
        "procurement_type",
        "procurement_sla",
        "shipping_provider",
        "local_shipping_fee",
        "zonal_shipping_fee",
        "national_shipping_fee",
    }
)
