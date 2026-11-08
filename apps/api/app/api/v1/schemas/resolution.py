"""The resolution read (SI11, ADR-0028 proposed): what Kuinn checked, changed and still needs for one
listing. Every model is a read: the engine itself runs inside the jobs, never from a request.

The vocabularies here are new (contract 2.1.0 MINOR); none widens an existing one.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.base import WireModel
from pydantic import AwareDatetime, Field

# Who handles an issue: Kuinn alone (an adapter repair, a deterministic content fix, an AI rewrite
# of one field, or a plain retry), the seller, the marketplace, or Kuinn's staff.
Lane = Literal["auto_repair", "auto_content", "ai_field", "seller", "marketplace", "retry", "staff"]
# Where the wrong value came from (shown so the seller knows whose data to fix).
Cause = Literal["seller_fact", "ai_content", "mapping", "image", "marketplace", "unknown"]
ResolutionState = Literal[
    "checking",
    "fixing",
    "resubmitted",
    "fix_ready",
    "needs_you",
    "needs_marketplace",
    "stopped",
    "live",
    "idle",
]
RemoteCheckStatus = Literal["pending", "clean", "issues", "failed"]
AttemptOutcome = Literal[
    # a marketplace dry run (precheck)
    "clean",
    "issues",
    "failed",
    # every listing_submissions.outcome (DATABASE.md §5.4)
    "pending",
    "accepted",
    "partially_accepted",
    "rejected",
    "error",
    "live",
    "superseded",
]
MAX_CYCLES: Final = 3


class RemoteCheckOut(WireModel):
    """The marketplace's dry-run verdict (Amazon VALIDATION_PREVIEW) on one version."""

    version_id: UUID
    status: RemoteCheckStatus
    at: AwareDatetime | None = None


class ChangeOut(WireModel):
    kind: Literal["repair", "content_fix", "ai_correction", "seller_answer"]
    field: Annotated[str, Field(min_length=1, max_length=192)]
    rule: str | None = Field(default=None, max_length=64)
    summary: Annotated[str, Field(min_length=1, max_length=300)]


class AttemptFailureOut(WireModel):
    field: Annotated[str, Field(min_length=1, max_length=192)]
    message: Annotated[str, Field(min_length=1, max_length=1000)]
    lane: Lane
    cause: Cause
    marketplace_code: str | None = Field(default=None, max_length=64)


class AttemptOut(WireModel):
    kind: Literal["precheck", "submission"]
    number: Annotated[int, Field(ge=0, le=99)] = Field(
        description="The submission attempt (1-3); 0 for a pre-check."
    )
    version: Annotated[int, Field(ge=1)]
    at: AwareDatetime
    actor: Literal["user", "system"]
    outcome: AttemptOutcome
    failed: Annotated[list[AttemptFailureOut], Field(max_length=50)] = Field(default_factory=list)
    changed: Annotated[list[ChangeOut], Field(max_length=50)] = Field(default_factory=list)


class NextStepOut(WireModel):
    kind: Literal["none", "wait", "approve", "answer", "marketplace", "resume", "regenerate"]
    task_ids: Annotated[list[UUID], Field(max_length=20)] = Field(default_factory=list)
    text: Annotated[str, Field(max_length=300)] = ""


class ResolutionOut(WireModel):
    state: ResolutionState
    cycles_used: Annotated[int, Field(ge=0, le=MAX_CYCLES)]
    max_cycles: Literal[3] = 3
    remote_check: RemoteCheckOut | None = None
    attempts: Annotated[list[AttemptOut], Field(max_length=10)] = Field(
        default_factory=list, description="Newest first."
    )
    next_step: NextStepOut
