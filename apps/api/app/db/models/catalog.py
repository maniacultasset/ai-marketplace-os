"""Catalog models: `products` (styles, variants and standalone products, with their fact
columns), `product_colourways` and `variant_matrix_cells` (DATABASE.md §5.2, §5.6; ADR-0007).
Columns, defaults, keys and indexes mirror revision 0001_core_tenancy under the same names; the
CHECK constraints live only in the revision."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base
from app.db.models.tenancy import (
    anchor,
    created_at_column,
    id_column,
    tenant_fk,
    to_membership,
    updated_at_column,
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
        Index(
            conv("ix_products_keyset"),
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(conv("ix_products_tenant_id_parent_product_id"), "tenant_id", "parent_product_id"),
        Index(
            conv("uq_products_parent_product_id_variant_key"),
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
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
