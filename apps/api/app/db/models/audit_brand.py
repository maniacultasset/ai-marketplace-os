"""`audit_log` (DATABASE.md §5.1), `brand_documents` and `brand_chunks` (§5.5). Columns,
defaults, keys and indexes mirror revisions 0002_listing_pipeline and 0002b_brand_chunks_hnsw
under the same names; the CHECK constraints live only in the revisions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
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

EMBEDDING_DIMENSIONS = 1536
# defaults named outside BrandChunk, whose `text` column shadows sqlalchemy.text in its body
ZERO = text("0")
TRUE = text("true")


class AuditLog(Base):
    """Append-only (0004), not partitioned. `tenant_id` is NULL only for a platform event."""

    __tablename__ = "audit_log"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_audit_log")),
        # NO ACTION (KI-B): a tenant with audit rows is purged by the S10 purge, not a cascade
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=conv("fk_audit_log_tenant_id_tenants")
        ),
        to_membership("audit_log", "actor_user_id"),
        Index(
            conv("ix_audit_log_tenant_id_occurred_at"),
            "tenant_id",
            text("occurred_at DESC"),
            text("id DESC"),
        ),
        Index(
            conv("ix_audit_log_tenant_id_entity"),
            "tenant_id",
            "entity_type",
            "entity_id",
            text("occurred_at DESC"),
        ),
        Index(
            conv("ix_audit_log_tenant_id_actor"),
            "tenant_id",
            "actor_user_id",
            text("occurred_at DESC"),
        ),
        Index(
            conv("ix_audit_log_platform"),
            text("occurred_at DESC"),
            postgresql_where=text("tenant_id IS NULL"),
        ),
        Index(conv("ix_audit_log_job_id"), "job_id", postgresql_where=text("job_id IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # personal data: a snapshot, redacted with the actor
    actor_email: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(Text)
    # untrusted text, never a prompt input
    reason: Mapped[str | None] = mapped_column(Text)
    # soft reference (no FK): job_runs are pruned sooner
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    ip: Mapped[str | None] = mapped_column(INET)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class BrandDocument(Base):
    __tablename__ = "brand_documents"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_brand_documents")),
        anchor("brand_documents"),
        UniqueConstraint(
            "tenant_id", "storage_key", name=conv("uq_brand_documents_tenant_id_storage_key")
        ),
        tenant_fk("brand_documents"),
        to_membership("brand_documents", "created_by"),
        Index(
            conv("uq_brand_documents_tenant_id_content_hash"),
            "tenant_id",
            "content_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            conv("ix_brand_documents_job_id"),
            "job_id",
            postgresql_where=text("job_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    filename: Mapped[str] = mapped_column(Text)
    # an object key, never a URL (D1: replaces the Canon's `gcs_uri`)
    storage_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'uploaded'"))
    # soft reference (no FK): job_runs are pruned sooner
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    content_type: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(Text)
    error_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class BrandChunk(Base):
    """Maps 1:1 to `BrandChunkDTO`; the text is untrusted prompt data."""

    __tablename__ = "brand_chunks"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_brand_chunks")),
        anchor("brand_chunks"),
        tenant_fk("brand_chunks"),
        Index(
            conv("uq_brand_chunks_active_source"),
            "tenant_id",
            "source_type",
            "source_id",
            "chunk_index",
            "embedding_model",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    type: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    embedding_model: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    # polymorphic, no FK: brand_document, brand_profile or listing_version
    source_type: Mapped[str] = mapped_column(Text)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    chunk_index: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    token_count: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=TRUE)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
