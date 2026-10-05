"""Fact-owner models: the `business_parties` address book and the 1:1 `brand_profiles`
(DATABASE.md §5.6). Columns, defaults, keys and indexes mirror revision 0001_core_tenancy under
the same names; the CHECK constraints live only in the revision."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    Text,
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
    updated_at_column,
)

# The match-or-create key of an import (DATABASE.md §5.6 `business_parties`); IMMUTABLE parts
# only, every nullable part in coalesce. Written as PostgreSQL deparses the revision's expression,
# so `alembic check` sees no difference. Changing it is never an in-place release (§8.3).
DEDUPE_KEY_SQL = (
    "CASE WHEN (legal_name IS NOT NULL) THEN "
    "((((((lower(btrim(legal_name)) || '|'::text) "
    "|| COALESCE(lower(regexp_replace(address_line1, '\\s+'::text, ' '::text, 'g'::text)), ''::text)) "
    "|| '|'::text) || COALESCE(postal_code, ''::text)) || '|'::text) || country_code) "
    "ELSE ((('cc|'::text || COALESCE(lower(email), ''::text)) || '|'::text) "
    "|| COALESCE(regexp_replace(phone, '[^0-9]'::text, ''::text, 'g'::text), ''::text)) "
    "END"
)

BRAND_PARTY_COLUMNS = (
    "legal_entity_party_id",
    "default_manufacturer_party_id",
    "default_marketer_party_id",
    "default_packer_party_id",
    "default_importer_party_id",
    "consumer_care_party_id",
)


def to_party(table: str, column: str) -> ForeignKeyConstraint:
    """`→ t` onto the address book: `(tenant_id, column) → business_parties (tenant_id, id)`,
    ON DELETE RESTRICT, MATCH SIMPLE."""
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["business_parties.tenant_id", "business_parties.id"],
        ondelete="RESTRICT",
        name=conv(f"fk_{table}_{column}"),
    )


class BusinessParty(Base):
    __tablename__ = "business_parties"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_business_parties")),
        anchor("business_parties"),
        tenant_fk("business_parties"),
        Index(
            conv("bp_dedupe_uq"),
            "tenant_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text))
    legal_name: Mapped[str | None] = mapped_column(Text)
    # Personal data under the DPDP Act: never granted to the read-only roles, never in a prompt.
    address_line1: Mapped[str | None] = mapped_column(Text)
    address_line2: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state_code: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(Text)
    country_code: Mapped[str] = mapped_column(Text, server_default=text("'IN'"))
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    gstin: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    evidence_ref: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text, Computed(DEDUPE_KEY_SQL, persisted=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class BrandProfile(Base):
    __tablename__ = "brand_profiles"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", name=conv("pk_brand_profiles")),
        # NO ACTION, not CASCADE: the purge deletes the profile before the tenant.
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=conv("fk_brand_profiles_tenant_id_tenants")
        ),
        *(to_party("brand_profiles", column) for column in BRAND_PARTY_COLUMNS),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    brand_names: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    brand_owner_type: Mapped[str | None] = mapped_column(Text)
    website_url: Mapped[str | None] = mapped_column(Text)
    brand_description: Mapped[str | None] = mapped_column(Text)
    logo_storage_key: Mapped[str | None] = mapped_column(Text)
    legal_entity_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    default_manufacturer_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    default_marketer_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    default_packer_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    default_importer_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    consumer_care_party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    default_country_of_origin: Mapped[str | None] = mapped_column(Text)
    content_locale: Mapped[str] = mapped_column(Text, server_default=text("'en-IN'"))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
