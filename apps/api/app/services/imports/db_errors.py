"""A database refusal of one import row, as that row's `ErrorItem` (S13 B9, Q7).

The message names the A.6 column from the fixed constraint table below, never the driver
message or the cell (both can echo values). The code comes from the constraint, or from the
guard SQLSTATE (S10's map, the 0006 PRV codes); anything else is `VALUE_NOT_ALLOWED`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorItem

from app.services.imports.row_model import row_error

E = ErrorCode

CONSTRAINTS: Final[Mapping[str, tuple[ErrorCode, str]]] = MappingProxyType(
    {
        "products_tenant_sku_idx": (E.DUPLICATE_SKU_IN_FILE, "seller_sku"),
        "ck_products_sku": (E.PATTERN_MISMATCH, "seller_sku"),
        "ck_products_title": (E.FIELD_TOO_LONG, "product_name"),
        "ck_products_category": (E.PATTERN_MISMATCH, "category"),
        "ck_products_brand_name": (E.FIELD_TOO_LONG, "brand_name"),
        "ck_products_sub_category": (E.PATTERN_MISMATCH, "sub_category"),
        "ck_products_product_type": (E.PATTERN_MISMATCH, "product_type"),
        "ck_products_gender": (E.VALUE_NOT_ALLOWED, "gender"),
        "ck_products_mrp": (E.VALUE_NOT_ALLOWED, "mrp"),
        "ck_products_selling_price": (E.VALUE_NOT_ALLOWED, "selling_price"),
        "ck_price_le_mrp": (E.PRICE_ABOVE_MRP, "selling_price"),
        "ck_products_tax_rate": (E.VALUE_NOT_ALLOWED, "tax_rate"),
        "ck_products_hsn_code": (E.PATTERN_MISMATCH, "hsn_code"),
        "ck_products_country_of_origin": (E.PATTERN_MISMATCH, "country_of_origin"),
        "ck_products_dims_all_or_none": (E.CROSS_FIELD_MISMATCH, "package_length_cm"),
        "ck_products_package_length_cm": (E.VALUE_NOT_ALLOWED, "package_length_cm"),
        "ck_products_package_breadth_cm": (E.VALUE_NOT_ALLOWED, "package_breadth_cm"),
        "ck_products_package_height_cm": (E.VALUE_NOT_ALLOWED, "package_height_cm"),
        "ck_products_package_weight_g": (E.VALUE_NOT_ALLOWED, "package_weight"),
        "ck_products_identifier_valid": (E.IDENTIFIER_INVALID, "product_identifier"),
        "ck_products_identifier_pair": (E.CROSS_FIELD_MISMATCH, "product_identifier"),
        "ck_products_identifier_not_on_style": (E.IDENTIFIER_INVALID, "product_identifier"),
        "products_identifier_uq": (E.IDENTIFIER_INVALID, "product_identifier"),
        "ck_products_style_owned": (E.CROSS_FIELD_MISMATCH, "parent_style_id"),
        "ck_products_source_image_urls": (E.VALUE_NOT_ALLOWED, "main_image"),
        "uq_products_tenant_id_parent_product_id_variant_key": (E.VARIATION_INVALID, "size_name"),
        "uq_variant_matrix_cells_tenant_id_variant_product_id": (E.VARIATION_INVALID, "size_name"),
        "vmc_cell_uq": (E.VARIATION_INVALID, "size_name"),
        "ck_product_colourways_colour_key": (E.FIELD_TOO_LONG, "color_name"),
        "ck_business_parties_legal_name": (E.FIELD_TOO_LONG, "manufacturer_name"),
        "ck_business_parties_address_line1": (E.FIELD_TOO_LONG, "manufacturer_address"),
        "ck_business_parties_email": (E.PATTERN_MISMATCH, "consumer_care_email"),
        "ck_business_parties_phone": (E.PATTERN_MISMATCH, "consumer_care_phone"),
        "ck_business_parties_postal_code": (E.PATTERN_MISMATCH, "manufacturer_address"),
    }
)
SQLSTATES: Final[Mapping[str, ErrorCode]] = MappingProxyType(
    {
        "VAR01": E.VARIATION_INVALID,
        "VAR02": E.VARIATION_INVALID,
        "GRD01": E.VARIATION_INVALID,  # the variant guard's refusals (parent, theme, axes)
        "PRV01": E.FACT_SOURCE_FORBIDDEN,
        "PRV02": E.UNKNOWN_FACT_KEY,
        "PRV03": E.FACT_LEVEL_INVALID,
    }
)
# dictionary key (or typed column) → the A.6 column a message names
FIELD_COLUMNS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "color": "color_name",
        "size": "size_name",
        "title": "product_name",
        "identifier": "product_identifier",
        "package_dimensions": "package_length_cm",
        "manufacturer": "manufacturer_name",
        "consumer_care": "consumer_care_email",
    }
)


def column_for_field(field_path: str) -> str:
    """`attributes.color` / `provenance.mrp` → the A.6 column (`color_name`, `mrp`)."""
    key = field_path.rsplit(".", 1)[-1]
    return FIELD_COLUMNS.get(key, key)


def row_db_item(sqlstate: str | None, constraint: str | None) -> ErrorItem:
    if constraint is not None and constraint in CONSTRAINTS:
        code, column = CONSTRAINTS[constraint]
        return row_error(code, f"{column} was refused by the database")
    if sqlstate is not None and sqlstate in SQLSTATES:
        return row_error(SQLSTATES[sqlstate], "the row's variant or fact data was refused")
    if sqlstate == "22003":
        return row_error(E.VALUE_NOT_ALLOWED, "a number in the row is out of range")
    return row_error(E.VALUE_NOT_ALLOWED, "the row was refused by the database")
