"""Human task inbox models (owner step S19; DATABASE.md §5.7 human_tasks).

`TaskOut` leaves out `evidence` (it may hold contact data) and `answer`, and `tenant_id`.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from mkos_schemas.base import StrictModel, WireModel
from pydantic import AwareDatetime, model_validator
from pydantic_core import PydanticCustomError

from app.api.pagination import Page
from app.api.v1.schemas.products import AnswerScalar

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


class TaskAnswerIn(StrictModel):
    """SI3: the seller's answer to a fact question: a `value`, or `skip: true`. Exactly one."""

    value: AnswerScalar | None = None
    skip: Literal[True] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> TaskAnswerIn:
        sent_null = any(
            name in self.model_fields_set and getattr(self, name) is None
            for name in ("value", "skip")
        )
        if sent_null or (self.value is None) == (self.skip is None):
            raise PydanticCustomError("mkos_invalid", "Send exactly one of `value` and `skip`.")
        return self
