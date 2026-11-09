"""Human task inbox models (owner step S19; DATABASE.md §5.7 human_tasks).

`TaskOut` leaves out `evidence` (it may hold contact data) and `answer`, and `tenant_id`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from mkos_schemas.base import StrictModel, WireModel
from pydantic import AwareDatetime, Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from app.api.pagination import Page
from app.api.v1.schemas.products import AnswerScalar
from app.core.text import has_control_char

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
    resolution: Literal["done", "dismissed"] | None = Field(
        default=None,
        description="SI9: how an account task (marketplace_error_review, permission_decision) "
        "was closed; null for every other task.",
    )
    blocking: bool = Field(
        default=False,
        description="An open listing ERROR waits on this answer, so the question blocks that "
        "listing (a photo question can too). Set on the list read; false once closed.",
    )
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


class TaskCloseIn(StrictModel):
    """SI9: close an account task (`marketplace_error_review`, `permission_decision`): `done`
    (the seller acted) or `dismissed` (nothing to do), with an optional note."""

    resolution: Literal["done", "dismissed"]
    note: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def _note_text(self) -> TaskCloseIn:
        if self.note is not None and (not self.note.strip() or has_control_char(self.note)):
            raise PydanticCustomError("mkos_invalid", "The note must be plain text.")
        return self
