"""`marketplace_accounts` (DATABASE.md §5.5), `images` (§5.2) and `batches` (§5.3). Columns,
defaults, keys and indexes mirror revision 0002_listing_pipeline under the same names; the CHECK
constraints live only in the revision."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base
from app.db.models.tenancy import (
    anchor,
    created_at_column,
    id_column,
    tenant_fk,
    to_membership,
    to_row,
    updated_at_column,
)

EMPTY_OBJECT = text("'{}'::jsonb")


class MarketplaceAccount(Base):
    __tablename__ = "marketplace_accounts"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_marketplace_accounts")),
        anchor("marketplace_accounts"),
        # the `(marketplace_account_id, marketplace)` target on listings
        UniqueConstraint("id", "marketplace", name=conv("uq_ma_id_marketplace")),
        tenant_fk("marketplace_accounts"),
        to_membership("marketplace_accounts", "connected_by_user_id"),
        to_membership("marketplace_accounts", "settings_updated_by"),
        Index(
            conv("uq_marketplace_accounts_tenant_id_marketplace"),
            "tenant_id",
            "marketplace",
            unique=True,
            postgresql_where=text("status <> 'revoked'"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    marketplace: Mapped[str] = mapped_column(Text)
    seller_id: Mapped[str] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    # KMS-wrapped AES-256-GCM token (invariant 6): never loaded unless asked for, never granted
    # to the read-only roles, decrypted only inside an adapter call.
    token_enc: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))
    kms_key_version: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    needs_reconnect_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refresh_error_code: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT)
    settings_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings_updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class Image(Base):
    __tablename__ = "images"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_images")),
        anchor("images"),
        UniqueConstraint("tenant_id", "storage_key", name=conv("uq_images_tenant_id_storage_key")),
        tenant_fk("images"),
        to_row("images", "product_id", "products"),
        to_row("images", "colourway_id", "product_colourways", ondelete="RESTRICT"),
        # product_id is the colourway's style (MATCH SIMPLE: no colourway, no check)
        ForeignKeyConstraint(
            ["colourway_id", "product_id"],
            ["product_colourways.id", "product_colourways.style_product_id"],
            ondelete="RESTRICT",
            name=conv("fk_images_colourway_id_product_id"),
        ),
        Index(
            conv("ix_images_product_id"), "product_id", postgresql_where=text("deleted_at IS NULL")
        ),
        Index(conv("ix_images_tenant_id_status"), "tenant_id", "status"),
        Index(
            conv("ix_images_tenant_id_colourway_id"),
            "tenant_id",
            "colourway_id",
            postgresql_where=text("colourway_id IS NOT NULL"),
        ),
        Index(
            conv("images_one_main"),
            "product_id",
            "colourway_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("slot = 'main' AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    colourway_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # an object key, never a URL; signed at read time
    storage_key: Mapped[str] = mapped_column(Text)
    slot: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'uploaded'"))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=EMPTY_OBJECT
    )
    compliance_status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    image_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    position: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    source_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_batches")),
        anchor("batches"),
        tenant_fk("batches"),
        to_membership("batches", "created_by_user_id"),
        Index(
            conv("ix_batches_tenant_id_created_at"),
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    item_count: Mapped[int] = mapped_column(Integer)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # soft reference: imports is 0003; set only on a `generate` batch
    import_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
