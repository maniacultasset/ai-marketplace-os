"""Batch-shaped approve and publish (DATABASE.md §7.1, T42; S3 D11).

API-local until `mkos_schemas.batches` exists (S2 Decision 2); the names are exactly DATABASE.md's
so S19's move keeps the OpenAPI component names. There is no `reviewer_id`: the approver is the
JWT subject, and a client-sent one is refused by `extra="forbid"`.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, Slug, StrictModel, WireModel
from mkos_schemas.enums import ListingStatus
from mkos_schemas.errors import ErrorReport
from pydantic import AfterValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from app.core.text import reject_control_chars

MAX_BATCH_ITEMS: Final = 200
# `ck_review_decisions_reason_code` (DATABASE.md:799); checked here so a bad reason is a 422, not a
# refused INSERT. Python-only: the declared contract keeps `reason_code` a slug.
REASON_CODES: Final = frozenset(
    {
        "inaccurate_fact",
        "off_brand_voice",
        "policy_violation",
        "poor_quality",
        "wrong_category",
        "image_issue",
        "other",
    }
)
Feedback = Annotated[NonBlankStr, Field(max_length=2000), AfterValidator(reject_control_chars)]
Instructions = Annotated[NonBlankStr, Field(max_length=2000), AfterValidator(reject_control_chars)]


def _unique_listings(ids: list[UUID]) -> None:
    if len(set(ids)) != len(ids):
        raise PydanticCustomError("mkos_invalid", "Each listing_id may appear once per batch.")


class ApproveItem(StrictModel):
    listing_id: UUID
    listing_version_id: UUID
    decision: Literal["approve", "reject"]
    reason_code: Slug | None = None
    feedback: Feedback | None = None
    regenerate: bool = False

    @model_validator(mode="after")
    def _reject_needs_reason(self) -> ApproveItem:
        if self.decision == "reject" and (self.reason_code is None or self.feedback is None):
            raise PydanticCustomError("mkos_invalid", "A reject needs reason_code and feedback.")
        if self.decision == "approve" and self.regenerate:
            raise PydanticCustomError("mkos_invalid", "regenerate applies to a reject only.")
        if self.reason_code is not None and self.reason_code not in REASON_CODES:
            raise PydanticCustomError("mkos_invalid", "Unknown reason_code.")
        return self


class ApproveRequest(StrictModel):
    items: Annotated[list[ApproveItem], Field(min_length=1, max_length=MAX_BATCH_ITEMS)]

    @model_validator(mode="after")
    def _unique(self) -> ApproveRequest:
        _unique_listings([item.listing_id for item in self.items])
        return self


class PublishItem(StrictModel):
    """S21 D4: `listing_version_id`, when given, must be the listing's current version (else the
    item is 409 `STALE_VERSION`), so a client publishes exactly what it reviewed."""

    listing_id: UUID
    listing_version_id: UUID | None = None


class PublishRequest(StrictModel):
    items: Annotated[list[PublishItem], Field(min_length=1, max_length=MAX_BATCH_ITEMS)]

    @model_validator(mode="after")
    def _unique(self) -> PublishRequest:
        _unique_listings([item.listing_id for item in self.items])
        return self


class RegenerateItem(StrictModel):
    """One item of S19's batch regenerate (Site L8121-8123). The path-scoped
    `POST /listings/{listing_id}/regenerate` takes `RegenerateRequest` instead."""

    listing_id: UUID
    instructions: Instructions | None = None


class BatchItemResult(WireModel):
    listing_id: UUID
    http_status: Annotated[int, Field(ge=100, le=599)]
    listing_status: ListingStatus | None = None
    job_id: UUID | None = None
    error: ErrorReport | None = None


class BatchResult(WireModel):
    batch_id: UUID
    items: list[BatchItemResult]


class BatchAccepted(WireModel):
    batch_id: UUID
    items: list[BatchItemResult]
    poll_url: NonBlankStr
