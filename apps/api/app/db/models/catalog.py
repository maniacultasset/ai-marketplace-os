"""Catalog models: `products` (styles, variants and standalone products, with their fact
columns), `product_colourways` and `variant_matrix_cells` (DATABASE.md §5.2, §5.6; ADR-0007).
Columns, defaults, keys and indexes mirror revision 0001_core_tenancy under the same names; the
CHECK constraints live only in the revision."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base
from app.db.models.facts import to_party
from app.db.models.tenancy import (
    anchor,
    created_at_column,
    id_column,
    tenant_fk,
    to_membership,
    updated_at_column,
)

PRODUCT_PARTY_COLUMNS = (
    "manufacturer_party_id",
    "marketer_party_id",
    "packer_party_id",
    "importer_party_id",
    "consumer_care_party_id",
)

EMPTY_OBJECT = text("'{}'::jsonb")
EMPTY_ARRAY = text("'[]'::jsonb")


def to_product(table: str, column: str) -> ForeignKeyConstraint:
    """`→ t` onto products: `(tenant_id, column) → products (tenant_id, id)`, ON DELETE RESTRICT."""
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["products.tenant_id", "products.id"],
        ondelete="RESTRICT",
        name=conv(f"fk_{table}_{column}"),
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_products")),
        anchor("products"),
        UniqueConstraint("tenant_id", "sku", name=conv("products_tenant_sku_idx")),
        tenant_fk("products"),
        to_product("products", "parent_product_id"),
        to_membership("products", "created_by"),
        *(to_party("products", column) for column in PRODUCT_PARTY_COLUMNS),
        Index(
            conv("products_identifier_uq"),
            "tenant_id",
            func.lpad(text("identifier_value"), 14, text("'0'")),
            unique=True,
            postgresql_where=text("identifier_value IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index(
            conv("ix_products_keyset"),
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(conv("ix_products_tenant_id_parent_product_id"), "tenant_id", "parent_product_id"),
        Index(
            conv("uq_products_tenant_id_parent_product_id_variant_key"),
            "tenant_id",
            "parent_product_id",
            "variant_key",
            unique=True,
            postgresql_where=text("parent_product_id IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    sku: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'draft'"))
    category: Mapped[str] = mapped_column(Text)
    brand_name: Mapped[str | None] = mapped_column(Text)
    style_number: Mapped[str | None] = mapped_column(Text)
    sub_category: Mapped[str | None] = mapped_column(Text)
    product_type: Mapped[str | None] = mapped_column(Text)
    gender: Mapped[str | None] = mapped_column(Text)
    stock_ref: Mapped[str | None] = mapped_column(Text)
    # Seller text: untrusted prompt data.
    description: Mapped[str | None] = mapped_column(Text)
    source_image_urls: Mapped[list[str]] = mapped_column(JSONB, server_default=EMPTY_ARRAY)
    parent_product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    variation_theme: Mapped[str | None] = mapped_column(Text)
    size_run: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    variant_attributes: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    variant_key: Mapped[str | None] = mapped_column(Text)
    # fact columns (DATABASE.md §5.6); each non-null one has its `provenance` key
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    selling_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(Text, server_default=text("'INR'"))
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    hsn_code: Mapped[str | None] = mapped_column(Text)
    country_of_origin: Mapped[str | None] = mapped_column(Text)
    generic_name: Mapped[str | None] = mapped_column(Text)
    net_quantity: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    net_quantity_unit: Mapped[str | None] = mapped_column(Text)
    manufacturer_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    marketer_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    packer_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    importer_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    consumer_care_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    identifier_type: Mapped[str | None] = mapped_column(Text)
    identifier_value: Mapped[str | None] = mapped_column(Text)
    identifier_status: Mapped[str | None] = mapped_column(Text)
    package_length_cm: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    package_breadth_cm: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    package_height_cm: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    package_weight_g: Mapped[int | None] = mapped_column(Integer)
    product_weight_g: Mapped[int | None] = mapped_column(Integer)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


# Written as PostgreSQL deparses the revision's expression, so `alembic check` sees no change.
COLOUR_KEY_SQL = "lower(btrim(((attributes -> 'color'::text) ->> 'value'::text)))"


class ProductColourway(Base):
    __tablename__ = "product_colourways"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_product_colourways")),
        anchor("product_colourways"),
        # the target of the same-style composite FKs (cells now, images in 0002)
        UniqueConstraint(
            "id", "style_product_id", name=conv("uq_product_colourways_id_style_product_id")
        ),
        tenant_fk("product_colourways"),
        to_product("product_colourways", "style_product_id"),
        Index(
            conv("uq_product_colourways_tenant_id_style_product_id_colour_key"),
            "tenant_id",
            "style_product_id",
            "colour_key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            conv("uq_product_colourways_tenant_id_colour_sku"),
            "tenant_id",
            "colour_sku",
            unique=True,
            postgresql_where=text("colour_sku IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index(
            conv("ix_product_colourways_tenant_id_style_product_id"),
            "tenant_id",
            "style_product_id",
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    style_product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    colour_key: Mapped[str] = mapped_column(Text, Computed(COLOUR_KEY_SQL, persisted=True))
    colour_sku: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(SmallInteger)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class VariantMatrixCell(Base):
    __tablename__ = "variant_matrix_cells"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_variant_matrix_cells")),
        anchor("variant_matrix_cells"),
        UniqueConstraint(
            "tenant_id",
            "style_product_id",
            "colourway_id",
            "size_key",
            name=conv("vmc_cell_uq"),
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint(
            "tenant_id",
            "variant_product_id",
            name=conv("uq_variant_matrix_cells_tenant_id_variant_product_id"),
        ),
        tenant_fk("variant_matrix_cells"),
        to_product("variant_matrix_cells", "style_product_id"),
        to_product("variant_matrix_cells", "variant_product_id"),
        ForeignKeyConstraint(
            ["colourway_id", "style_product_id"],
            ["product_colourways.id", "product_colourways.style_product_id"],
            ondelete="RESTRICT",
            name=conv("fk_variant_matrix_cells_colourway_id_style_product_id"),
        ),
        Index(
            conv("ix_variant_matrix_cells_tenant_id_style_product_id"),
            "tenant_id",
            "style_product_id",
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    style_product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    colourway_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    size_key: Mapped[str | None] = mapped_column(Text)
    availability: Mapped[str] = mapped_column(Text)
    variant_product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    unavailable_reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    evidence_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
