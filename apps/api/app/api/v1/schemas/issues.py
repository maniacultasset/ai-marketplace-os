"""Issue inbox models (owner step S19; DATABASE.md §5.7 listing_issues).

`IssueOut` is the `listing_issues` row a seller sees: the internal keys (`dedupe_key`, `rule_id`,
`error_map_id`, the validation-run and job ids) and `tenant_id` stay out.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from mkos_schemas.base import WireModel
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, FixAction, Marketplace, RetryClass, Severity
from pydantic import AwareDatetime

from app.api.pagination import Page

IssueStatus = Literal["open", "auto_fixed", "resolved", "dismissed"]
IssueResolution = Literal[
    "deterministic_fix",
    "ai_correction",
    "human_answer",
    "human_edit",
    "regenerated",
    "revalidated_clean",
    "marketplace_cleared",
    "accepted_warning",
]


class IssueOut(WireModel):
    id: UUID
    listing_id: UUID
    marketplace: Marketplace
    listing_item_id: UUID | None = None
    listing_version_id: UUID | None = None
    source: ErrorSource
    field_path: str
    error_code: ErrorCode
    error_class: ErrorClass
    retry_class: RetryClass
    severity: Severity
    auto_fixable: bool
    fix_action: FixAction | None = None
    marketplace_code: str | None = None
    raw_message: str | None = None
    message: str
    plain_message: str | None = None
    suggestion: str | None = None
    value_submitted: str | None = None
    occurrences: int
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    status: IssueStatus
    resolution: IssueResolution | None = None
    action_taken: str | None = None
    resolved_at: AwareDatetime | None = None
    resolved_by_user_id: UUID | None = None
    human_task_id: UUID | None = None
    recurrence_of_id: UUID | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class IssuePage(Page[IssueOut]):
    pass
