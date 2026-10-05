"""Tenancy models: `tenants`, global `users`, `tenant_memberships`, `invitations`
(DATABASE.md §5.1; ADR-0006). Columns, defaults, keys and indexes mirror revision
0001_core_tenancy under the same names; the CHECK constraints live only in the revision."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base

GEN_UUID = text("gen_random_uuid()")
NOW = text("now()")


def id_column() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, server_default=GEN_UUID)


def created_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=NOW)


def updated_at_column() -> Mapped[datetime]:
    """Moved on every UPDATE by the `set_updated_at` trigger."""
    return mapped_column(DateTime(timezone=True), server_default=NOW)


def tenant_fk(table: str) -> ForeignKeyConstraint:
    """The std tenant FK: `tenant_id → tenants(id) ON DELETE CASCADE` (DATABASE.md §1)."""
    return ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.id"],
        ondelete="CASCADE",
        name=conv(f"fk_{table}_tenant_id_tenants"),
    )


def anchor(table: str) -> UniqueConstraint:
    """The composite-FK anchor `UNIQUE (tenant_id, id)`."""
    return UniqueConstraint("tenant_id", "id", name=conv(f"uq_{table}_tenant_id_id"))


def to_membership(table: str, column: str) -> ForeignKeyConstraint:
    """`→ m`: `(tenant_id, column) → tenant_memberships (tenant_id, user_id)`, MATCH SIMPLE."""
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["tenant_memberships.tenant_id", "tenant_memberships.user_id"],
        name=conv(f"fk_{table}_{column}"),
    )


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_tenants")),
        UniqueConstraint("slug", name=conv("uq_tenants_slug")),
    )

    id: Mapped[uuid.UUID] = id_column()
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text)
    tone: Mapped[str | None] = mapped_column(Text)
    guidelines: Mapped[list[str] | None] = mapped_column(JSONB)
    glossary: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    plan: Mapped[str | None] = mapped_column(Text)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_publish: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    llm_daily_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    offboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_users")),
        Index(conv("uq_users_email_lower"), func.lower(text("email")), unique=True),
    )

    id: Mapped[uuid.UUID] = id_column()
    email: Mapped[str] = mapped_column(Text)
    # argon2id; never loaded unless asked for, never granted to the read-only roles.
    hashed_password: Mapped[str | None] = deferred(mapped_column(Text))
    auth_provider: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    display_name: Mapped[str | None] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_tenant_memberships")),
        anchor("tenant_memberships"),
        UniqueConstraint(
            "tenant_id", "user_id", name=conv("uq_tenant_memberships_tenant_id_user_id")
        ),
        tenant_fk("tenant_memberships"),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name=conv("fk_tenant_memberships_user_id_users"),
        ),
        to_membership("tenant_memberships", "invited_by_user_id"),
        Index(
            conv("ix_tenant_memberships_user_id_active"),
            "user_id",
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    role: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_invitations")),
        anchor("invitations"),
        UniqueConstraint("token_hash", name=conv("uq_invitations_token_hash")),
        tenant_fk("invitations"),
        to_membership("invitations", "invited_by_user_id"),
        ForeignKeyConstraint(
            ["accepted_user_id"], ["users.id"], name=conv("fk_invitations_accepted_user_id_users")
        ),
        Index(
            conv("uq_invitations_pending"),
            "tenant_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    email: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    # sha256 of the raw token; never granted to the read-only roles.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
