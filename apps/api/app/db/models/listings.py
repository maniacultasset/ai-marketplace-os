"""`listings`, `listing_versions`, `review_decisions` and `listing_items` (DATABASE.md §5.3). Columns, defaults, keys and indexes
mirror revision 0002_listing_pipeline under the same names; the CHECK constraints live only in
the revision.

`→ lv` = `(listing_id, col) → listing_versions (listing_id, id)`: it pins a version to its own
listing. The listing's own pointers use `(id, col)`, are DEFERRABLE INITIALLY IMMEDIATE and are
added by ALTER TABLE after `listing_versions` exists (`use_alter`)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
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
    to_row,
    updated_at_column,
)

VERSION_POINTERS = (
    "current_version_id",
    "approved_version_id",
    "published_version_id",
    "live_version_id",
)


def to_version(
    table: str, column: str, *, listing_column: str = "listing_id", deferrable: bool = False
) -> ForeignKeyConstraint:
    """`→ lv`: `(listing_column, column) → listing_versions (listing_id, id)`."""
    return ForeignKeyConstraint(
        [listing_column, column],
        ["listing_versions.listing_id", "listing_versions.id"],
        name=conv(f"fk_{table}_{column}"),
        deferrable=True if deferrable else None,
        initially="IMMEDIATE" if deferrable else None,
        use_alter=deferrable,
    )


def to_listing_marketplace(table: str) -> ForeignKeyConstraint:
    """`→ l/mkt`: `(listing_id, marketplace) → listings (id, marketplace)`."""
    return ForeignKeyConstraint(
        ["listing_id", "marketplace"],
        ["listings.id", "listings.marketplace"],
        name=conv(f"fk_{table}_listing_id_marketplace"),
    )


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_listings")),
        anchor("listings"),
        UniqueConstraint(
            "product_id", "marketplace", name=conv("uq_listings_product_id_marketplace")
        ),
        # the `→ l/mkt` target
        UniqueConstraint("id", "marketplace", name=conv("uq_listings_id_marketplace")),
        tenant_fk("listings"),
        to_row("listings", "product_id", "products"),
        to_row("listings", "marketplace_account_id", "marketplace_accounts"),
        ForeignKeyConstraint(
            ["marketplace_account_id", "marketplace"],
            ["marketplace_accounts.id", "marketplace_accounts.marketplace"],
            name=conv("fk_listings_marketplace_account_id_marketplace"),
        ),
        to_membership("listings", "paused_by_user_id"),
        to_membership("listings", "archived_by_user_id"),
        *(
            to_version("listings", pointer, listing_column="id", deferrable=True)
            for pointer in VERSION_POINTERS
        ),
        Index(
            conv("ix_listings_tenant_id_status_marketplace"), "tenant_id", "status", "marketplace"
        ),
        Index(
            conv("ix_listings_review_queue"),
            "tenant_id",
            "status_changed_at",
            "id",
            postgresql_where=text(
                "status IN ('validated', 'needs_review') AND paused_at IS NULL "
                "AND archived_at IS NULL"
            ),
        ),
        Index(
            conv("ix_listings_human_action"),
            "tenant_id",
            "status_reason",
            text("status_changed_at DESC"),
            postgresql_where=text("status IN ('blocked', 'failed')"),
        ),
        Index(
            conv("ix_listings_tenant_id_marketplace_marketplace_state"),
            "tenant_id",
            "marketplace",
            "marketplace_state",
        ),
        Index(
            conv("ix_listings_last_activity"),
            "tenant_id",
            text("last_event_at DESC"),
            text("id DESC"),
        ),
        Index(
            conv("ix_listings_tenant_id_marketplace_external_id"),
            "tenant_id",
            "marketplace",
            "external_id",
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index(
            conv("ix_listings_sync_due"),
            "next_sync_at",
            postgresql_where=text(
                "next_sync_at IS NOT NULL AND paused_at IS NULL AND archived_at IS NULL"
            ),
        ),
        Index(
            conv("ix_listings_ops_queue"),
            "status",
            "status_changed_at",
            postgresql_where=text("status IN ('blocked', 'failed', 'publishing')"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    marketplace: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'created'"))
    status_reason: Mapped[str | None] = mapped_column(Text)
    last_event: Mapped[str] = mapped_column(Text, server_default=text("'create'"))
    last_event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    category: Mapped[str] = mapped_column(Text)
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    approved_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    live_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_id: Mapped[str | None] = mapped_column(Text)
    marketplace_sku: Mapped[str | None] = mapped_column(Text)
    marketplace_state: Mapped[str] = mapped_column(Text, server_default=text("'not_submitted'"))
    marketplace_state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # built by the adapter, never user input
    live_url: Mapped[str | None] = mapped_column(Text)
    live_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_poll_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    marketplace_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class ListingVersion(Base):
    """Immutable except the one-time approval (lv_guard, 0004)."""

    __tablename__ = "listing_versions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_listing_versions")),
        anchor("listing_versions"),
        # the target of every `→ lv`
        UniqueConstraint("listing_id", "id", name=conv("uq_listing_versions_listing_id_id")),
        UniqueConstraint(
            "listing_id", "version", name=conv("listing_versions_listing_version_idx")
        ),
        tenant_fk("listing_versions"),
        to_row("listing_versions", "listing_id", "listings"),
        to_membership("listing_versions", "generated_by_user_id"),
        to_membership("listing_versions", "edited_by_user_id"),
        to_membership("listing_versions", "approved_by_user_id"),
        to_version("listing_versions", "parent_version_id"),
        Index(
            conv("ix_listing_versions_job_id"),
            "job_id",
            postgresql_where=text("job_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    image_reports_json: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    generated_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    edited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    origin: Mapped[str] = mapped_column(Text)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    changed_paths: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    locale: Mapped[str] = mapped_column(Text, server_default=text("'en-IN'"))
    rules_version: Mapped[int | None] = mapped_column(Integer)
    # FK to category_templates arrives in 0003
    category_template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # soft reference (no FK): job_runs are pruned sooner
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    attempts_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = created_at_column()


class ReviewDecision(Base):
    """Append-only (0004): one row per decision on an exact version."""

    __tablename__ = "review_decisions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_review_decisions")),
        anchor("review_decisions"),
        tenant_fk("review_decisions"),
        to_row("review_decisions", "listing_id", "listings"),
        to_version("review_decisions", "listing_version_id"),
        to_membership("review_decisions", "decided_by_user_id"),
        to_row("review_decisions", "batch_id", "batches"),
        Index(
            conv("uq_review_decisions_listing_version_id"),
            "listing_version_id",
            unique=True,
            postgresql_where=text("decision = 'approve'"),
        ),
        Index(
            conv("ix_review_decisions_tenant_id_listing_id_decided_at"),
            "tenant_id",
            "listing_id",
            text("decided_at DESC"),
        ),
        Index(conv("ix_review_decisions_tenant_id_batch_id"), "tenant_id", "batch_id"),
        Index(
            conv("ix_review_decisions_regeneration_job_id"),
            "regeneration_job_id",
            postgresql_where=text("regeneration_job_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    decision: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    # untrusted text, inside the prompt-injection boundary
    feedback: Mapped[str | None] = mapped_column(Text)
    field_paths: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    instructions: Mapped[str | None] = mapped_column(Text)
    facts_digest: Mapped[bytes | None] = mapped_column(LargeBinary)
    decided_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # soft reference (no FK): job_runs are pruned sooner
    regeneration_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class ListingItem(Base):
    """One row per sellable marketplace SKU (T45)."""

    __tablename__ = "listing_items"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_listing_items")),
        anchor("listing_items"),
        # the target of listing_issues' `(listing_item_id, listing_id)` FK
        UniqueConstraint("id", "listing_id", name=conv("uq_listing_items_id_listing_id")),
        UniqueConstraint(
            "listing_id", "product_id", name=conv("uq_listing_items_listing_id_product_id")
        ),
        UniqueConstraint(
            "tenant_id",
            "marketplace",
            "seller_sku",
            name=conv("uq_listing_items_tenant_id_marketplace_seller_sku"),
        ),
        tenant_fk("listing_items"),
        to_row("listing_items", "listing_id", "listings"),
        to_listing_marketplace("listing_items"),
        to_row("listing_items", "product_id", "products"),
        to_version("listing_items", "live_version_id", deferrable=True),
        Index(
            conv("uq_listing_items_one_root"),
            "listing_id",
            unique=True,
            postgresql_where=text("item_role IN ('standalone', 'parent')"),
        ),
        Index(
            conv("ix_listing_items_tenant_id_listing_id_item_role"),
            "tenant_id",
            "listing_id",
            "item_role",
        ),
        Index(
            conv("ix_listing_items_tenant_id_marketplace_external_id"),
            "tenant_id",
            "marketplace",
            "external_id",
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    marketplace: Mapped[str] = mapped_column(Text)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    item_role: Mapped[str] = mapped_column(Text)
    seller_sku: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    live_url: Mapped[str | None] = mapped_column(Text)
    marketplace_state: Mapped[str] = mapped_column(Text, server_default=text("'not_submitted'"))
    live_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    first_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    live_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
