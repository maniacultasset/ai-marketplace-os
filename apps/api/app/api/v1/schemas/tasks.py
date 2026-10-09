"""Human task inbox models (owner step S19; DATABASE.md §5.7 human_tasks).

`TaskOut` leaves out `evidence` (it may hold contact data) and `answer`, and `tenant_id`.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from mkos_schemas.base import WireModel
from pydantic import AwareDatetime

from app.api.pagination import Page

TaskStatus = Literal["open", "answered", "applied", "cancelled"]
TaskKind = Literal[
    "missing_fact",
    "conflicting_facts",
    "unverified_declaration",
    "approval_or_evidence",
    "permission_decision",
    "reconnect_account",
    "image_replacement",
    "marketplace_error_review",
]


class TaskOut(WireModel):
    id: UUID
    kind: TaskKind
    product_id: UUID | None = None
    colourway_id: UUID | None = None
    target_path: str | None = None
    marketplace_account_id: UUID | None = None
    image_id: UUID | None = None
    question: str
    reason: str
    expected_answer: dict[str, Any]
    suggestions: list[Any]
    status: TaskStatus
    answered_by_user_id: UUID | None = None
    answered_at: AwareDatetime | None = None
    resume_from: Literal["generate", "validate", "publish"]
    applied_at: AwareDatetime | None = None
    resume_batch_id: UUID | None = None
    cancelled_reason: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class TaskPage(Page[TaskOut]):
    pass
