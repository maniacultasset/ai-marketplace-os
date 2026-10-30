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
from types import MappingProxyType
from typing import Any

from sqlalchemy import (
    DateTime,
    FetchedValue,
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
    event,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, Mapper, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base, request_id_column, traceparent_column
from app.db.models._tenant import TenantScoped
from app.db.models.listings import to_listing_marketplace, to_version
from app.db.models.tenancy import (
    NOW,
    anchor,
    created_at_column,
    id_column,
    tenant_fk,
    to_membership,
    to_row,
)


class JobRun(TenantScoped, Base):
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
    __mapper_args__ = MappingProxyType({"eager_defaults": True})

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    # set by the BEFORE INSERT trigger job_runs_set_queue(), whatever the caller sends;
    # FetchedValue + eager_defaults make the INSERT ... RETURNING load the trigger's value, and
    # _drop_caller_queue() keeps a caller's value from shadowing it on the object
    queue: Mapped[str] = mapped_column(Text, server_default=FetchedValue())
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
    request_id: Mapped[str | None] = request_id_column()
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    error_code: Mapped[str | None] = mapped_column(Text)
    cost_reserved_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0009 (S23): the enqueuing request's W3C trace context (CHECK ck_job_runs_traceparent_format)
    traceparent: Mapped[str | None] = traceparent_column()


@event.listens_for(JobRun, "before_insert")
def _drop_caller_queue(_mapper: Mapper[JobRun], _connection: Any, target: JobRun) -> None:
    """The trigger overwrites any queue the caller set; drop it from the pending state so the
    ORM treats `queue` as server-generated and loads the trigger's value from RETURNING."""
    inspect(target).dict.pop("queue", None)


class Import(TenantScoped, Base):
    __tablename__ = "imports"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_imports")),
        anchor("imports"),
        tenant_fk("imports"),
        to_row("imports", "job_id", "job_runs"),
        to_membership("imports", "created_by"),
        Index(conv("ix_imports_job_id"), "job_id"),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    storage_key: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    total_rows: Mapped[int | None] = mapped_column(Integer)
    imported_rows: Mapped[int] = mapped_column(Integer)
    failed_rows: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_column()


class ImportRow(TenantScoped, Base):
    __tablename__ = "import_rows"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_import_rows")),
        anchor("import_rows"),
        UniqueConstraint(
            "tenant_id",
            "import_id",
            "row_number",
            name=conv("uq_import_rows_tenant_id_import_id_row_number"),
        ),
        tenant_fk("import_rows"),
        to_row("import_rows", "import_id", "imports", ondelete="CASCADE"),
        # a deleted product clears only product_id (PG 15+), never tenant_id
        to_row("import_rows", "product_id", "products", ondelete="SET NULL (product_id)"),
        Index(
            conv("ix_import_rows_tenant_id_import_id_status"),
            "tenant_id",
            "import_id",
            "status",
            "row_number",
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    import_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    row_number: Mapped[int] = mapped_column(Integer)
    sku: Mapped[str | None] = mapped_column(Text)
    parent_sku: Mapped[str | None] = mapped_column(Text)
    # cells as uploaded: untrusted, may hold party contact data; no read-only grant
    raw_row: Mapped[dict[str, Any]] = mapped_column(JSONB, deferred=True)
    status: Mapped[str] = mapped_column(Text)
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    error_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # soft reference (DATABASE.md §1)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = created_at_column()


class IdempotencyKey(TenantScoped, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "key", name=conv("pk_idempotency_keys")),
        tenant_fk("idempotency_keys"),
        to_membership("idempotency_keys", "user_id"),
        Index(conv("ix_idempotency_keys_expires_at"), "expires_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    key: Mapped[str] = mapped_column(Text)
    endpoint: Mapped[str] = mapped_column(Text)
    # sha256 of endpoint + body; never granted to the read-only roles
    request_hash: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()


class RefreshToken(TenantScoped, Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_refresh_tokens")),
        anchor("refresh_tokens"),
        # looked up by hash before any tenant is known
        UniqueConstraint("token_hash", name=conv("uq_refresh_tokens_token_hash")),
        tenant_fk("refresh_tokens"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.user_id"],
            ondelete="CASCADE",
            name=conv("fk_refresh_tokens_user_id"),
        ),
        # rotation never links across tenants; deleting a successor clears only replaced_by
        to_row(
            "refresh_tokens", "replaced_by", "refresh_tokens", ondelete="SET NULL (replaced_by)"
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # sha256 of the raw token; never granted to the read-only roles
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    # 0011 (DPP-1): how the family was proven, copied on every rotation
    amr: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    mfa_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0013 (ADR-0023): the trusted browser that began the family and when (no foreign key: a
    # removed browser must not turn its sessions into ordinary ones)
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    device_auth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthState(TenantScoped, Base):
    __tablename__ = "oauth_states"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_oauth_states")),
        anchor("oauth_states"),
        # the callback carries no JWT: the row is found by hash, the tenant comes from it
        UniqueConstraint("state_hash", name=conv("uq_oauth_states_state_hash")),
        tenant_fk("oauth_states"),
        to_membership("oauth_states", "user_id"),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # sha256 of the raw state; never granted to the read-only roles
    state_hash: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    marketplace: Mapped[str] = mapped_column(Text)
    # the PKCE verifier; never granted to the read-only roles
    code_verifier: Mapped[str | None] = mapped_column(Text, deferred=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    # 0006 (S15): a pending row holds the sealed token pair between the callback and the confirm;
    # a state row leaves all six NULL. Never granted to the read-only roles.
    token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    kms_key_version: Mapped[str | None] = mapped_column(Text)
    seller_id: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RateLimitCounter(Base):
    """Counters only: no tenant_id, no RLS, UNLOGGED (empty after a crash)."""

    __tablename__ = "rate_limit_counters"
    __table_args__ = (
        PrimaryKeyConstraint("bucket_key", "window_start", name=conv("pk_rate_limit_counters")),
        {"prefixes": ["UNLOGGED"]},
    )

    bucket_key: Mapped[str] = mapped_column(Text)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    n: Mapped[int] = mapped_column(Integer)


def to_submission_anchor(table: str, column: str) -> ForeignKeyConstraint:
    """`(column, listing_id, listing_version_id) → listing_submissions (id, listing_id,
    listing_version_id)`: the same listing and version as the submission it names."""
    return ForeignKeyConstraint(
        [column, "listing_id", "listing_version_id"],
        [
            "listing_submissions.id",
            "listing_submissions.listing_id",
            "listing_submissions.listing_version_id",
        ],
        name=conv(f"fk_{table}_{column}"),
    )


class ListingSubmission(TenantScoped, Base):
    __tablename__ = "listing_submissions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_listing_submissions")),
        anchor("listing_submissions"),
        # the previous-attempt and per-field target
        UniqueConstraint(
            "id",
            "listing_id",
            "listing_version_id",
            name=conv("uq_listing_submissions_id_listing_id_listing_version_id"),
        ),
        UniqueConstraint(
            "listing_id",
            "listing_version_id",
            "attempt",
            name=conv("uq_listing_submissions_listing_id_listing_version_id_attempt"),
        ),
        UniqueConstraint(
            "tenant_id",
            "marketplace",
            "idempotency_key",
            name=conv("uq_listing_submissions_tenant_id_marketplace_idempotency_key"),
        ),
        tenant_fk("listing_submissions"),
        to_row("listing_submissions", "listing_id", "listings"),
        to_version("listing_submissions", "listing_version_id"),
        to_listing_marketplace("listing_submissions"),
        to_row("listing_submissions", "marketplace_account_id", "marketplace_accounts"),
        ForeignKeyConstraint(
            ["marketplace_account_id", "marketplace"],
            ["marketplace_accounts.id", "marketplace_accounts.marketplace"],
            name=conv("fk_listing_submissions_marketplace_account_id_marketplace"),
        ),
        to_row("listing_submissions", "job_id", "job_runs"),
        to_membership("listing_submissions", "requested_by_user_id"),
        to_submission_anchor("listing_submissions", "previous_submission_id"),
        # at most one open submission per listing
        Index(
            conv("ux_ls_open"),
            "listing_id",
            unique=True,
            postgresql_where=text("outcome IN ('pending', 'accepted')"),
        ),
        Index(
            conv("ix_listing_submissions_attempts"),
            "tenant_id",
            "listing_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(conv("ix_listing_submissions_job_id"), "job_id"),
        # 0009 (S23): publish success rate for analytics
        Index(
            conv("ix_listing_submissions_tenant_id_responded_at"),
            "tenant_id",
            "responded_at",
            postgresql_include=["outcome", "marketplace"],
            postgresql_where=text("responded_at IS NOT NULL"),
        ),
        Index(
            conv("ix_listing_submissions_submission_ref"),
            "tenant_id",
            "marketplace",
            "submission_ref",
            postgresql_where=text("submission_ref IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    marketplace: Mapped[str] = mapped_column(Text)
    marketplace_account_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    attempt: Mapped[int] = mapped_column(SmallInteger)
    operation: Mapped[str] = mapped_column(Text)
    field_paths: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    payload_sha256: Mapped[bytes] = mapped_column(LargeBinary)
    payload_storage_key: Mapped[str] = mapped_column(Text)
    facts_digest: Mapped[bytes] = mapped_column(LargeBinary)
    idempotency_key: Mapped[str] = mapped_column(Text)
    submission_ref: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    error_code: Mapped[str | None] = mapped_column(Text)
    retry_justification: Mapped[str | None] = mapped_column(Text)
    previous_submission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    corrections_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    actor_type: Mapped[str] = mapped_column(Text)
    error_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_response_sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    raw_response_storage_key: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()


class PublishFieldResult(TenantScoped, Base):
    """Append-only per-field ledger, keyed by child SKU (the listing item)."""

    __tablename__ = "publish_field_results"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_publish_field_results")),
        anchor("publish_field_results"),
        UniqueConstraint(
            "submission_id",
            "listing_item_id",
            "field",
            name=conv("uq_publish_field_results_submission_id_listing_item_id_field"),
        ),
        tenant_fk("publish_field_results"),
        to_row("publish_field_results", "submission_id", "listing_submissions"),
        ForeignKeyConstraint(
            ["submission_id", "listing_id", "listing_version_id"],
            [
                "listing_submissions.id",
                "listing_submissions.listing_id",
                "listing_submissions.listing_version_id",
            ],
            name=conv("fk_publish_field_results_submission_id_listing_id"),
        ),
        ForeignKeyConstraint(
            ["listing_item_id", "listing_id"],
            ["listing_items.id", "listing_items.listing_id"],
            name=conv("fk_publish_field_results_listing_item_id"),
        ),
        Index(
            conv("ix_publish_field_results_retry"),
            "tenant_id",
            "listing_id",
            "listing_version_id",
            "listing_item_id",
            "field",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_item_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # a mkos_schemas.paths field path (the revision's CHECK mirrors FIELD_PATH_PATTERNS)
    field: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    marketplace_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
