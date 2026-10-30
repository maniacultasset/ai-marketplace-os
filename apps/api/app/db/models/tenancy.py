"""Tenancy models: `tenants`, global `users`, `tenant_memberships`, `invitations`
(DATABASE.md §5.1; ADR-0006), the platform `signup_requests` (0010; ADR-0019), the per-user MFA,
challenge and password-history tables (0011; DPP-1, ADR-0021), the Google sign-in flows
(0012; ADR-0020) and the trusted browsers (0013; ADR-0023). Columns, defaults, keys and indexes mirror revision
0001_core_tenancy under the same names; the CHECK constraints live only in the revision."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base
from app.db.models._tenant import TenantScoped

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


def to_row(
    table: str, column: str, target: str, *, ondelete: str | None = None
) -> ForeignKeyConstraint:
    """`→ t`: `(tenant_id, column) → target (tenant_id, id)`, NO ACTION unless `ondelete`."""
    return ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{target}.tenant_id", f"{target}.id"],
        ondelete=ondelete,
        name=conv(f"fk_{table}_{column}"),
    )


class Tenant(TenantScoped, Base):
    __tenant_column__ = "id"
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
        # 0012 (Google sign-in, ADR-0020): one Google account per user, one user per account
        Index(
            conv("uq_users_google_sub"),
            "google_sub",
            unique=True,
            postgresql_where=text("google_sub IS NOT NULL"),
        ),
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
    # 0010: set only by staff (`user make-admin`); a trigger refuses any other writer.
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # 0011 (DPP-1): the lockout counter and lock move only through the definer functions.
    failed_auth_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=NOW
    )
    password_must_change: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # 0012 (ADR-0020): the linked Google account's `sub`; written only by the definer functions
    # (users_credential_guard), never granted to the read-only roles.
    google_sub: Mapped[str | None] = mapped_column(Text)


def _user_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["user_id"], ["users.id"], ondelete="CASCADE", name=conv(f"fk_{table}_user_id_users")
    )


class UserMfaFactor(Base):
    """A TOTP factor (0011): one pending and one active per user. The secret is one sealed
    envelope blob (`app.core.crypto.seal_bytes`), never loaded unless asked for."""

    __tablename__ = "user_mfa_factors"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_user_mfa_factors")),
        _user_fk("user_mfa_factors"),
        Index(
            conv("uq_user_mfa_factors_active"),
            "user_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            conv("uq_user_mfa_factors_pending"),
            "user_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text, server_default=text("'totp'"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    secret_blob: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    key_version: Mapped[str] = mapped_column(Text)
    last_used_step: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at_column()
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserRecoveryCode(Base):
    """One recovery code (0011): `sha256(user_id || code)` only, used once."""

    __tablename__ = "user_recovery_codes"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_user_recovery_codes")),
        _user_fk("user_recovery_codes"),
        UniqueConstraint("code_hash", name=conv("uq_user_recovery_codes_code_hash")),
        Index(
            conv("ix_user_recovery_codes_user_id"),
            "user_id",
            postgresql_where=text("used_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    code_hash: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()


class MfaChallenge(Base):
    """A sign-in second step (0011), found by the sha256 of its opaque token."""

    __tablename__ = "mfa_challenges"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_mfa_challenges")),
        _user_fk("mfa_challenges"),
        UniqueConstraint("challenge_hash", name=conv("uq_mfa_challenges_challenge_hash")),
        Index(conv("ix_mfa_challenges_user_id"), "user_id"),
    )

    id: Mapped[uuid.UUID] = id_column()
    challenge_hash: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    purpose: Mapped[str] = mapped_column(Text)
    amr: Mapped[list[str]] = mapped_column(ARRAY(Text))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    # 0012 (ADR-0020): a Google account to link once this password sign-in finishes
    link_google_sub: Mapped[str | None] = mapped_column(Text)
    # 0013 (ADR-0023): "trust this browser", chosen at the code step (NULL = false)
    trust_device: Mapped[bool | None] = mapped_column(Boolean)


class TrustedDevice(Base):
    """A trusted browser (0013; ADR-0023): one per browser and user, found by the sha256 of its
    random token, which changes on every use (the previous hash catches a replayed one). A
    platform table under FORCE RLS (`td_self`); the hashes are never loaded unless asked for and
    never granted to the read-only roles."""

    __tablename__ = "trusted_devices"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_trusted_devices")),
        _user_fk("trusted_devices"),
        UniqueConstraint("token_hash", name=conv("uq_trusted_devices_token_hash")),
        Index(conv("ix_trusted_devices_user_id_created_at"), "user_id", "created_at"),
        Index(conv("ix_trusted_devices_expires_at"), "expires_at"),
    )

    id: Mapped[uuid.UUID] = id_column()
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    token_hash: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    prev_token_hash: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))
    method: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    trusted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()


class UserPasswordHistory(Base):
    """A previous password hash (0011): the 9 before the current one are kept."""

    __tablename__ = "user_password_history"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_user_password_history")),
        _user_fk("user_password_history"),
        Index(
            conv("ix_user_password_history_user_id_created_at"),
            "user_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    hashed_password: Mapped[str] = deferred(mapped_column(Text, nullable=False))
    created_at: Mapped[datetime] = created_at_column()


class SignupRequest(Base):
    """A self-serve brand sign-up awaiting a platform admin (0010; ADR-0019). A platform table:
    no `tenant_id` and not `TenantScoped`; `created_tenant_id` is set on approve."""

    __tablename__ = "signup_requests"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_signup_requests")),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name=conv("fk_signup_requests_user_id_users"),
        ),
        ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.id"],
            name=conv("fk_signup_requests_decided_by_user_id_users"),
        ),
        ForeignKeyConstraint(
            ["created_tenant_id"],
            ["tenants.id"],
            ondelete="SET NULL",
            name=conv("fk_signup_requests_created_tenant_id_tenants"),
        ),
        Index(
            conv("uq_signup_requests_pending"),
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(conv("ix_signup_requests_status_created_at"), "status", "created_at", "id"),
        Index(conv("ix_signup_requests_user_id"), "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = id_column()
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    brand_name: Mapped[str] = mapped_column(Text)
    owner_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    reason: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    # 0012 (ADR-0020): `google` when Google verified the applicant's email; set at insert only
    verified_via: Mapped[str | None] = mapped_column(Text)


class LoginFlow(Base):
    """One Google sign-in between the browser's redirects (0012; ADR-0020). A platform table: no
    `tenant_id` and not `TenantScoped`; FORCE RLS lets app_rw read and delete only the row whose
    `flow_hash` is `app.token_hash`. Hashes, the verifier and the identity are never granted to
    the read-only roles."""

    __tablename__ = "login_flows"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_login_flows")),
        UniqueConstraint("flow_hash", name=conv("uq_login_flows_flow_hash")),
        Index(conv("ix_login_flows_expires_at"), "expires_at"),
    )

    id: Mapped[uuid.UUID] = id_column()
    phase: Mapped[str] = mapped_column(Text)
    flow_hash: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    binding_hash: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    code_verifier: Mapped[str | None] = deferred(mapped_column(Text))
    nonce_hash: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))
    sub: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    hd: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    given_name: Mapped[str | None] = mapped_column(Text)
    family_name: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()


class TenantMembership(TenantScoped, Base):
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


class Invitation(TenantScoped, Base):
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
