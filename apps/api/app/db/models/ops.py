"""The tenant tables of revision 0003_ops_tables: `job_runs`, `imports`, `import_rows`,
`idempotency_keys`, `oauth_states` (DATABASE.md §5.4), `refresh_tokens` (§5.1),
`listing_submissions` and `publish_field_results` (§5.3), and the tenant-less
`rate_limit_counters` (§5.4). Columns, defaults, keys and indexes mirror the revision under the
same names; the CHECK constraints, `job_runs`' fillfactor and its `queue` trigger live only in
the revision."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
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

from app.db.base import Base
from app.db.models.tenancy import NOW, anchor, id_column, tenant_fk, to_membership, to_row


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_job_runs")),
        anchor("job_runs"),
        # one requeue per job (a requeue inserts a new row, SUPERSEDED.md:73)
        UniqueConstraint("parent_job_id", name=conv("uq_job_runs_parent_job_id")),
        tenant_fk("job_runs"),
        to_row("job_runs", "listing_id", "listings"),
        to_membership("job_runs", "created_by"),
        to_row("job_runs", "batch_id", "batches"),
        # parent → self, kept in the tenant on the row's own anchor
        to_row("job_runs", "parent_job_id", "job_runs"),
        Index(
            conv("ux_job_runs_live"),
            "tenant_id",
            "listing_id",
            "kind",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            conv("ux_job_runs_idempotency_key"),
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            conv("ix_job_runs_lease"),
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            conv("ix_job_runs_dead_letter_tenant"),
            "tenant_id",
            text("finished_at DESC"),
            postgresql_where=text("status = 'dead_letter'"),
        ),
        Index(
            conv("ix_job_runs_dead_letter"),
            text("finished_at DESC"),
            postgresql_where=text("status = 'dead_letter'"),
        ),
        Index(
            conv("ix_job_runs_batch"),
            "tenant_id",
            "batch_id",
            postgresql_where=text("batch_id IS NOT NULL"),
        ),
        Index(conv("ix_job_runs_recent"), "tenant_id", text("queued_at DESC"), text("id DESC")),
        Index(conv("ix_job_runs_entity"), "tenant_id", "entity_type", "entity_id"),
        # `WITH (fillfactor = 80)` lives in the revision only: SQLAlchemy 2.0 has no Table
        # storage-parameter option, and Alembic does not compare reloptions.
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    # set by the BEFORE INSERT trigger job_runs_set_queue(), whatever the caller sends
    queue: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    node: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    queue_job_id: Mapped[str | None] = mapped_column(Text)
    listing_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    request_id: Mapped[str | None] = mapped_column(Text)
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    error_code: Mapped[str | None] = mapped_column(Text)
    cost_reserved_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
