"""`listing_repairs` (SI11, revision 0022_autonomous_resolution; ADR-0028 proposed). Columns, keys
and indexes mirror the revision under the same names; the CHECK constraints and the retire-once
guard live only in the revision.

A repair is adapter data, never listing content: the marketplace adapter applies it to one
attribute only while the seller's source value still hashes to `source_digest`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base
from app.db.models._tenant import TenantScoped
from app.db.models.tenancy import anchor, created_at_column, id_column, tenant_fk, to_row

REPAIR_RULES = (
    "enum_fold",
    "unit_format",
    "identifier_normalize",
    "exemption_flag",
    "fill_from_facts",
    "drop_locked",
    "image_slot",
    "fill_variant_axis",  # SI13 (0024)
)
RETIRED_REASONS = ("source_changed", "superseded", "regenerated")


class ListingRepair(TenantScoped, Base):
    __tablename__ = "listing_repairs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_listing_repairs")),
        anchor("listing_repairs"),
        tenant_fk("listing_repairs"),
        to_row("listing_repairs", "listing_id", "listings", ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["listing_id", "marketplace"],
            ["listings.id", "listings.marketplace"],
            ondelete="CASCADE",
            name=conv("fk_listing_repairs_listing_id_marketplace"),
        ),
        ForeignKeyConstraint(
            ["listing_item_id", "listing_id"],
            ["listing_items.id", "listing_items.listing_id"],
            ondelete="CASCADE",
            name=conv("fk_listing_repairs_listing_item_id_listing_id"),
        ),
        Index(
            conv("uq_listing_repairs_live"),
            "listing_id",
            "listing_item_id",
            "attribute",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
        Index(conv("uq_listing_repairs_once"), "listing_id", "issue_key", "rule", unique=True),
        Index(
            conv("ix_listing_repairs_listing"),
            "tenant_id",
            "listing_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    marketplace: Mapped[str] = mapped_column(Text)
    listing_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    field_path: Mapped[str] = mapped_column(Text)
    attribute: Mapped[str] = mapped_column(Text)
    rule: Mapped[str] = mapped_column(Text)
    issue_key: Mapped[str] = mapped_column(Text)
    # soft references, no FK: job rows are pruned at 90 days, a repair lives with its listing
    source_issue_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source_digest: Mapped[bytes] = mapped_column(LargeBinary)
    to_digest: Mapped[bytes] = mapped_column(LargeBinary)
    # the attribute's marketplace value list; NULL for drop_locked (never granted to the read roles)
    value_json: Mapped[Any] = mapped_column(JSONB, nullable=True)
    alias_version: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = created_at_column()
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(Text)
    # 0038 (client item 20): a suggested fix found while auto-fix is off; never applied until the
    # seller releases it (true -> false once, `listing_repairs_guard`)
    held: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
