"""`human_tasks` and `listing_issues` (DATABASE.md §5.7). Columns, defaults, keys and indexes
mirror revision 0002_listing_pipeline under the same names; the CHECK constraints live only in
the revision. `listing_issues.rule_id` is the documented plain FK; its `human_task_id`,
`recurrence_of_id`, run ids, `job_id` and `error_map_id` are soft references."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from app.db.base import Base
from app.db.models.listings import to_listing_marketplace, to_version
from app.db.models.tenancy import (
    anchor,
    created_at_column,
    id_column,
    tenant_fk,
    to_membership,
    to_row,
    updated_at_column,
)

EMPTY_ARRAY = text("'[]'::jsonb")
LIVE_TASK = "status IN ('open', 'answered')"


class HumanTask(Base):
    __tablename__ = "human_tasks"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_human_tasks")),
        anchor("human_tasks"),
        tenant_fk("human_tasks"),
        to_row("human_tasks", "product_id", "products"),
        to_row("human_tasks", "colourway_id", "product_colourways"),
        to_row("human_tasks", "marketplace_account_id", "marketplace_accounts"),
        to_row("human_tasks", "image_id", "images"),
        to_membership("human_tasks", "answered_by_user_id"),
        to_row("human_tasks", "resume_batch_id", "batches"),
        # one question per fact across both marketplaces and every variant
        Index(
            conv("ht_one_live_fact"),
            "tenant_id",
            "product_id",
            "colourway_id",
            "target_path",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text(
                f"{LIVE_TASK} AND target_path IS NOT NULL AND marketplace_account_id IS NULL"
            ),
        ),
        Index(
            conv("ht_one_live_account"),
            "tenant_id",
            "marketplace_account_id",
            "kind",
            "target_path",
            unique=True,
            postgresql_where=text(f"{LIVE_TASK} AND marketplace_account_id IS NOT NULL"),
        ),
        Index(
            conv("ix_human_tasks_inbox"),
            "tenant_id",
            "status",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(conv("ix_human_tasks_tenant_id_product_id"), "tenant_id", "product_id"),
        Index(
            conv("ix_human_tasks_tenant_id_resume_batch_id"),
            "tenant_id",
            "resume_batch_id",
            postgresql_where=text("resume_batch_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    colourway_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    target_path: Mapped[str | None] = mapped_column(Text)
    marketplace_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    image_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    question: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # suggestions, evidence and answer may hold contact data: no read-only-role grant
    suggestions: Mapped[list[Any]] = mapped_column(JSONB, server_default=EMPTY_ARRAY)
    evidence: Mapped[list[Any]] = mapped_column(JSONB, server_default=EMPTY_ARRAY)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    answer: Mapped[Any] = mapped_column(JSONB, nullable=True)
    answered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_from: Mapped[str] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    cancelled_reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class ListingIssue(Base):
    __tablename__ = "listing_issues"
    __table_args__ = (
        PrimaryKeyConstraint("id", name=conv("pk_listing_issues")),
        anchor("listing_issues"),
        tenant_fk("listing_issues"),
        to_row("listing_issues", "listing_id", "listings"),
        to_listing_marketplace("listing_issues"),
        ForeignKeyConstraint(
            ["listing_item_id", "listing_id"],
            ["listing_items.id", "listing_items.listing_id"],
            name=conv("fk_listing_issues_listing_item_id_listing_id"),
        ),
        to_version("listing_issues", "listing_version_id"),
        # the documented plain FK: a composite one can't reach a platform rule (NULL tenant)
        ForeignKeyConstraint(
            ["rule_id"], ["rules.id"], name=conv("fk_listing_issues_rule_id_rules")
        ),
        to_membership("listing_issues", "resolved_by_user_id"),
        # added by 0003 (NOT VALID + VALIDATE); error-map rows are never deleted (§5.2)
        ForeignKeyConstraint(
            ["error_map_id"], ["marketplace_error_map.id"], name=conv("li_error_map_fk")
        ),
        # the issue sync's ON CONFLICT target
        Index(
            conv("li_one_open"),
            "listing_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index(
            conv("ix_listing_issues_recurrence"),
            "listing_id",
            "dedupe_key",
            text("resolved_at DESC"),
        ),
        Index(
            conv("ix_listing_issues_open_by_listing"),
            "tenant_id",
            "listing_id",
            postgresql_where=text("status = 'open'"),
        ),
        Index(
            conv("ix_listing_issues_open_inbox"),
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("status = 'open'"),
        ),
        Index(
            conv("ix_listing_issues_inbox"),
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(
            conv("ix_listing_issues_tenant_id_error_code"),
            "tenant_id",
            "error_code",
            text("created_at DESC"),
        ),
        Index(
            conv("ix_listing_issues_human_task_id"),
            "human_task_id",
            postgresql_where=text("human_task_id IS NOT NULL"),
        ),
        Index(
            conv("ix_listing_issues_recurrence_of_id"),
            "recurrence_of_id",
            postgresql_where=text("recurrence_of_id IS NOT NULL"),
        ),
        Index(
            conv("ix_listing_issues_job_id"),
            "job_id",
            postgresql_where=text("job_id IS NOT NULL"),
        ),
        Index(
            conv("li_unknown_codes"),
            "marketplace",
            "marketplace_code",
            postgresql_where=text("error_class = 'unknown' AND status = 'open'"),
        ),
    )

    id: Mapped[uuid.UUID] = id_column()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    marketplace: Mapped[str] = mapped_column(Text)
    listing_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    listing_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    validation_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    last_validation_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source: Mapped[str] = mapped_column(Text)
    field_path: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str] = mapped_column(Text)
    error_class: Mapped[str] = mapped_column(Text)
    retry_class: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    auto_fixable: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    fix_action: Mapped[str | None] = mapped_column(Text)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # FK to marketplace_error_map arrives in 0003
    error_map_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    marketplace_code: Mapped[str | None] = mapped_column(Text)
    # exactly as the marketplace returned it: untrusted
    raw_message: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    plain_message: Mapped[str | None] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(Text)
    value_submitted: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text)
    occurrences: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    resolution: Mapped[str | None] = mapped_column(Text)
    action_taken: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # soft references: closed issues and terminal tasks are pruned at 13 months
    human_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    recurrence_of_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
