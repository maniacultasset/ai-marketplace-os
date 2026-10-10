"""`validation_runs` (DATABASE.md §5.3), `agent_runs` (§5.4) and `performance` (§5.5). Columns,
defaults, keys and indexes mirror revision 0002_listing_pipeline under the same names; the CHECK
constraints live only in the revision. Template pins and job ids are soft references here."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base, request_id_column
from app.db.models._tenant import TenantScoped
from app.db.models.listings import to_listing_marketplace, to_version
from app.db.models.tenancy import (
    anchor,
    created_at_column,
    id_column,
    tenant_fk,
    to_row,
    updated_at_column,
)


class ValidationRun(TenantScoped, Base):
    """Append-only (0004): one row per stage per attempt, in the one ErrorReport shape."""

    __tablename__ = "validation_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_validation_runs")),
        anchor("validation_runs"),
        tenant_fk("validation_runs"),
        to_row("validation_runs", "listing_id", "listings"),
        to_version("validation_runs", "listing_version_id"),
        # added by 0003 (NOT VALID + VALIDATE): the template pin, on `ct_id_version`
        ForeignKeyConstraint(
            ["category_template_id", "template_version"],
            ["category_templates.id", "category_templates.version"],
            name=conv("fk_validation_runs_category_template_id_category_templates"),
        ),
        Index(
            conv("ix_validation_runs_listing_id_created_at"),
            "listing_id",
            text("created_at DESC"),
        ),
        Index(conv("ix_validation_runs_listing_version_id"), "listing_version_id"),
        Index(
            conv("ix_validation_runs_failed"),
            "tenant_id",
            "source",
            text("created_at DESC"),
            postgresql_where=text("NOT passed"),
        ),
        Index(
            conv("validation_runs_errors_gin"),
            "error_report_json",
            postgresql_using="gin",
            postgresql_ops={"error_report_json": "jsonb_path_ops"},
            postgresql_where=text("NOT passed"),
        ),
        # 0009 (S23): first-pass validation rate for analytics
        Index(
            conv("ix_validation_runs_tenant_id_created_at_first_pass"),
            "tenant_id",
            "created_at",
            postgresql_include=["passed", "marketplace"],
            postgresql_where=text("source = 'rules_engine' AND attempt = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    marketplace: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    passed: Mapped[bool] = mapped_column(Boolean)
    error_report_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attempt: Mapped[int] = mapped_column(Integer)
    rules_version: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str | None] = request_id_column()
    # `(category_template_id, template_version) → category_templates` arrives in 0003
    category_template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    template_version: Mapped[int | None] = mapped_column(Integer)
    facts_digest: Mapped[bytes | None] = mapped_column(LargeBinary)
    blocking_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    warning_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = created_at_column()


class AgentRun(TenantScoped, Base):
    """Append-only (0004): one row per model call (invariant 7), written only by the backend."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_agent_runs")),
        anchor("agent_runs"),
        tenant_fk("agent_runs"),
        to_row("agent_runs", "product_id", "products"),
        to_row("agent_runs", "listing_id", "listings"),
        to_version("agent_runs", "listing_version_id"),
        Index(
            conv("ix_agent_runs_tenant_id_started_at"),
            "tenant_id",
            "started_at",
            postgresql_include=["cost_usd"],
        ),
        Index(conv("ix_agent_runs_started_at"), "started_at", postgresql_include=["cost_usd"]),
        Index(conv("ix_agent_runs_job_id"), "job_id"),
        Index(
            conv("agent_runs_product_created_idx"),
            "product_id",
            "created_at",
            postgresql_include=["cost_usd", "tokens_in", "tokens_out", "duration_ms"],
            postgresql_where=text("product_id IS NOT NULL"),
        ),
        Index(
            conv("agent_runs_errors_gin"),
            "error_report_json",
            postgresql_using="gin",
            postgresql_ops={"error_report_json": "jsonb_path_ops"},
            postgresql_where=text("error_report_json IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    listing_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    listing_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # soft reference (no FK): agent_runs outlive job_runs
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    trigger: Mapped[str] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    tokens_cached_in: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    duration_ms: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text)
    error_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    request_id: Mapped[str] = request_id_column()
    agent_name: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    prompt_version: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_digest: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class Performance(TenantScoped, Base):
    __tablename__ = "performance"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_performance")),
        anchor("performance"),
        UniqueConstraint("listing_id", "date", name=conv("performance_listing_date_key")),
        tenant_fk("performance"),
        # D7: beside `→ l/mkt`, so a row can't name another tenant's listing
        to_row("performance", "listing_id", "listings"),
        to_listing_marketplace("performance"),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    marketplace: Mapped[str] = mapped_column(Text)
    date: Mapped[date_type] = mapped_column(Date)
    impressions: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    clicks: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    orders: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    returns: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    ranking_position: Mapped[int | None] = mapped_column(Integer)
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(Text, server_default=text("'INR'"))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
