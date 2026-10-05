"""`rules` (DATABASE.md §5.2; CANON-ADDENDUM.md A§4): platform rules (`tenant_id` NULL) and brand
rules, with `rule_type`, `subject` and `pack` promoted from `rule_def` into the scope key, and
`revision` drawn from `rules_revision_seq`. Columns, defaults, keys and indexes mirror revision
0002_listing_pipeline under the same names; the CHECK constraints live only in the revision."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base
from app.db.models.tenancy import created_at_column, id_column, updated_at_column

ZERO_UUID = text("'00000000-0000-0000-0000-000000000000'::uuid")


class Rule(Base):
    __tablename__ = "rules"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_rules")),
        # CASCADE: a tenant's brand rules go with the tenant (A§4 had NO ACTION)
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name=conv("fk_rules_tenant_id_tenants"),
        ),
        Index(
            conv("rules_scope_key"),
            "marketplace",
            func.coalesce(text("category"), text("''")),
            "field",
            "rule_type",
            "subject",
            "pack",
            "version",
            func.coalesce(text("tenant_id"), ZERO_UUID),
            unique=True,
        ),
        Index(
            conv("rules_lookup_idx"), "marketplace", "category", postgresql_where=text("enabled")
        ),
        Index(
            conv("rules_tenant_idx"),
            "tenant_id",
            "marketplace",
            postgresql_where=text("tenant_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    marketplace: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    field: Mapped[str] = mapped_column(Text)
    rule_type: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, server_default=text("''"))
    pack: Mapped[str] = mapped_column(Text, server_default=text("'core'"))
    severity: Mapped[str] = mapped_column(Text, server_default=text("'error'"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Written as Alembic renders the reflected default (it drops `::regclass`), so `alembic
    # check` compares the strings and never evaluates nextval() to compare.
    revision: Mapped[int] = mapped_column(
        Integer, server_default=text("nextval('rules_revision_seq')")
    )
    rule_def: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
