"""The platform-global tables of revision 0003_ops_tables: `category_templates` and
`marketplace_error_map` (DATABASE.md §5.2), and the attribute dictionary `attribute_definitions`,
`attribute_columns` and `category_attributes` (§5.6), plus revision 0004_integrity's
`listing_status_transitions` (§6.1). None has `tenant_id`; the migrator's
`app.cli` loaders write them. Columns, defaults, keys and indexes mirror the revision under the
same names; the CHECK constraints live only in the revision."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base
from app.db.models.tenancy import created_at_column, id_column

# Written as Alembic renders the reflected default (it drops `::regclass`); see `Rule.revision`.
RULES_REVISION = text("nextval('rules_revision_seq')")
FALSE = text("false")


def to_definition(table: str) -> ForeignKeyConstraint:
    """`attribute_key → attribute_definitions (key) ON DELETE RESTRICT`."""
    return ForeignKeyConstraint(
        ["attribute_key"],
        ["attribute_definitions.key"],
        ondelete="RESTRICT",
        name=conv(f"fk_{table}_attribute_key_attribute_definitions"),
    )


class CategoryTemplate(Base):
    __tablename__ = "category_templates"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_category_templates")),
        UniqueConstraint("marketplace", "category", "version", name=conv("ct_version_key")),
        # the `(category_template_id, template_version)` target on validation_runs
        UniqueConstraint("id", "version", name=conv("ct_id_version")),
        Index(
            conv("ct_one_current"),
            "marketplace",
            "category",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    marketplace: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean)
    product_type: Mapped[str] = mapped_column(Text)
    # adapter-only (invariant 4)
    external_category_id: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    source_version: Mapped[str | None] = mapped_column(Text)
    source_digest: Mapped[bytes | None] = mapped_column(LargeBinary)
    raw_schema_key: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_column()


class MarketplaceErrorMap(Base):
    __tablename__ = "marketplace_error_map"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_marketplace_error_map")),
        UniqueConstraint("marketplace", "match_on", "raw_value", "version", name=conv("mem_key")),
        Index(
            conv("ix_marketplace_error_map_lookup"),
            "marketplace",
            "match_on",
            "raw_value",
            text("version DESC"),
            postgresql_where=text("enabled"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    marketplace: Mapped[str] = mapped_column(Text)
    match_on: Mapped[str] = mapped_column(Text)
    raw_value: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str] = mapped_column(Text)
    error_class: Mapped[str] = mapped_column(Text)
    retry_class: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    auto_fixable: Mapped[bool] = mapped_column(Boolean)
    fix_action: Mapped[str | None] = mapped_column(Text)
    field_hint: Mapped[str | None] = mapped_column(Text)
    plain_text: Mapped[str] = mapped_column(Text)
    seller_action: Mapped[str | None] = mapped_column(Text)
    policy_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = created_at_column()


class AttributeDefinition(Base):
    __tablename__ = "attribute_definitions"
    __table_args__ = (PrimaryKeyConstraint("key", name=conv("pk_attribute_definitions")),)

    key: Mapped[str] = mapped_column(Text)
    section: Mapped[str] = mapped_column(Text)
    storage: Mapped[str] = mapped_column(Text)
    owner_levels: Mapped[list[str]] = mapped_column(ARRAY(Text))
    value_type: Mapped[str] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(Text)
    vocabulary: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    input_aliases: Mapped[list[str]] = mapped_column(ARRAY(Text))
    is_critical: Mapped[bool] = mapped_column(Boolean)
    allowed_sources: Mapped[list[str]] = mapped_column(ARRAY(Text))
    min_confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    confirm_agent_captures: Mapped[bool] = mapped_column(Boolean)
    brd_ref: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean)
    revision: Mapped[int] = mapped_column(Integer, server_default=RULES_REVISION)
    created_at: Mapped[datetime] = created_at_column()


class AttributeColumn(Base):
    __tablename__ = "attribute_columns"
    __table_args__ = (
        PrimaryKeyConstraint("table_name", "column_name", name=conv("pk_attribute_columns")),
        to_definition("attribute_columns"),
    )

    table_name: Mapped[str] = mapped_column(Text)
    column_name: Mapped[str] = mapped_column(Text)
    attribute_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class CategoryAttribute(Base):
    __tablename__ = "category_attributes"
    __table_args__ = (
        PrimaryKeyConstraint("category", "attribute_key", name=conv("pk_category_attributes")),
        to_definition("category_attributes"),
    )

    category: Mapped[str] = mapped_column(Text)
    attribute_key: Mapped[str] = mapped_column(Text)
    relevance: Mapped[str] = mapped_column(Text)
    # NULL or true: a category may only tighten criticality
    critical_override: Mapped[bool | None] = mapped_column(Boolean)
    display_order: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean)
    revision: Mapped[int] = mapped_column(Integer, server_default=RULES_REVISION)
    created_at: Mapped[datetime] = created_at_column()


class ListingStatusTransition(Base):
    """`listing_status_transitions` (revision 0004_integrity, DATABASE.md §6.1): the 63 seeded
    status edges the listings status guard reads. Platform-global, SELECT-only for the app roles,
    changed only by migration; its CHECK constraints live in the revision."""

    __tablename__ = "listing_status_transitions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "from_status", "to_status", "event", name=conv("pk_listing_status_transitions")
        ),
    )

    from_status: Mapped[str] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text)
    event: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    requires_current_approval: Mapped[bool] = mapped_column(Boolean, server_default=FALSE)
    voids_approval: Mapped[bool] = mapped_column(Boolean, server_default=FALSE)
    allowed_reasons: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    requires_from_reason: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
