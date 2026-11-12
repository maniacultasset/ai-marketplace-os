"""The issue history read (client feedback item 20): every marketplace issue of one listing, who
handles it, and each fix Kuinn applied with its before and after values, its reason and its
outcome. A read only: the engine runs inside the jobs, never from a request.

The vocabularies here are new (contract 2.1.0 MINOR); none widens an existing one.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.base import WireModel
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace
from pydantic import AwareDatetime, Field

from app.api.v1.schemas.issues import IssueStatus
from app.api.v1.schemas.resolution import AttemptOutcome, Lane

# The client's error taxonomy (feedback item 20), one group per stored error class
ErrorGroup = Literal[
    "content_data",
    "missing_attribute",
    "image",
    "identifier_exemption",
    "category_mismatch",
    "account_permission",
    "marketplace_system",
    "unknown",
]
# Who settles an issue: Kuinn on its own, the seller in Kuinn, the seller on the marketplace (or
# the marketplace itself), or Kuinn's team
Handler = Literal["kuinn", "you", "marketplace", "staff"]
FixKind = Literal["repair", "content_fix", "ai_correction"]
# What became of a fix: not sent yet, sent and waiting, accepted, refused again, or replaced (the
# seller changed the value, or a newer send superseded it)
FixOutcome = Literal["waiting", "sent", "accepted", "refused", "replaced"]


class HistoryIssueOut(WireModel):
    id: UUID
    field: Annotated[str, Field(min_length=1, max_length=192)]
    error_code: ErrorCode
    group: ErrorGroup
    handler: Handler
    lane: Lane | None = None
    status: IssueStatus
    marketplace_code: str | None = Field(default=None, max_length=64)
    marketplace_message: str | None = Field(
        default=None, max_length=1000, description="The marketplace's own words, scrubbed."
    )
    cause: Annotated[str, Field(min_length=1, max_length=1000)] = Field(
        description="What went wrong, in plain words."
    )
    action: str | None = Field(
        default=None, max_length=1000, description="What the seller does, when it is theirs."
    )
    value_submitted: str | None = Field(default=None, max_length=500)
    occurrences: Annotated[int, Field(ge=1)]
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    resolved_at: AwareDatetime | None = None


class FixOut(WireModel):
    id: UUID
    issue_id: UUID | None = None
    kind: FixKind
    field: Annotated[str, Field(min_length=1, max_length=192)]
    rule: str | None = Field(default=None, max_length=64)
    before: str | None = Field(
        default=None, max_length=500, description="The value the marketplace refused."
    )
    after: str | None = Field(
        default=None, max_length=500, description="The value Kuinn sends now (null: not shown)."
    )
    reason: Annotated[str, Field(min_length=1, max_length=300)]
    outcome: FixOutcome
    at: AwareDatetime
    suggested: bool = Field(
        default=False,
        description=(
            "Client item 20: with the brand's auto-fix off, Kuinn only suggests this fix; it is "
            "`waiting` until the seller applies it (`POST /listings/{id}/fixes/apply`)."
        ),
    )


class HistoryAttemptOut(WireModel):
    kind: Literal["precheck", "submission"]
    number: Annotated[int, Field(ge=0, le=99)]
    version: Annotated[int, Field(ge=1)]
    at: AwareDatetime
    actor: Literal["user", "system"]
    outcome: AttemptOutcome
    issue_ids: Annotated[list[UUID], Field(max_length=50)] = Field(
        default_factory=list, description="The issues first seen on this attempt."
    )
    fix_ids: Annotated[list[UUID], Field(max_length=50)] = Field(
        default_factory=list, description="The fixes Kuinn made after this attempt."
    )


class HistoryCountsOut(WireModel):
    fixed: Annotated[int, Field(ge=0)] = 0
    fixing: Annotated[int, Field(ge=0)] = 0
    needs_you: Annotated[int, Field(ge=0)] = 0
    marketplace: Annotated[int, Field(ge=0)] = 0
    staff: Annotated[int, Field(ge=0)] = 0


class IssueHistoryOut(WireModel):
    marketplace: Marketplace
    counts: HistoryCountsOut
    issues: Annotated[list[HistoryIssueOut], Field(max_length=100)] = Field(
        default_factory=list, description="Every marketplace ERROR of the listing, newest first."
    )
    fixes: Annotated[list[FixOut], Field(max_length=100)] = Field(
        default_factory=list, description="Every fix Kuinn made, newest first."
    )
    attempts: Annotated[list[HistoryAttemptOut], Field(max_length=10)] = Field(
        default_factory=list, description="Newest first."
    )
