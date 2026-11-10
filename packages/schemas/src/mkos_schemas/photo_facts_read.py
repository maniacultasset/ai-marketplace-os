"""AI seam payloads for one photo-facts read on its own (V1.1: read the photos on upload).

The same read the listing pipeline makes in its `image_agent` stage (SI3, `photo_facts/v3`), as an
entry point of its own: when the seller adds a front, back or close-up photo to a product, the
backend sends up to 4 product photos (vision copies, front first) and the keys to read; the
pipeline answers with its screened observations and the records of its call. Nothing here writes
a fact: the backend screens the observations again (`app/services/photo_facts.py::screen`) and
stores only unconfirmed `ai` values the seller can confirm or reject, or questions. Critical keys
are never targets.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

from pydantic import ConfigDict, Field

from .agent import AgentRunRecord
from .base import SCHEMA_VERSION, RequestId, Slug, WireModel
from .errors import ErrorReport
from .jobs import MAX_PHOTO_OBSERVATIONS, ContextImage, PhotoObservation, PhotoTarget

__all__ = [
    "MAX_PHOTO_READ_IMAGES",
    "MAX_PHOTO_READ_TARGETS",
    "PhotoFactsReadRequest",
    "PhotoFactsReadResult",
]

MAX_PHOTO_READ_IMAGES: Final = 4
MAX_PHOTO_READ_TARGETS: Final = 16


class PhotoFactsReadRequest(WireModel):
    """One read of a product's photos for the given keys."""

    model_config = WireModel.model_config | ConfigDict(hide_input_in_errors=True)

    schema_version: str = SCHEMA_VERSION
    tenant_id: UUID
    product_id: UUID
    job_id: UUID
    request_id: RequestId
    attempt: Annotated[int, Field(ge=1, le=3)] = 1
    images: Annotated[list[ContextImage], Field(min_length=1, max_length=MAX_PHOTO_READ_IMAGES)]
    targets: Annotated[list[PhotoTarget], Field(min_length=1, max_length=MAX_PHOTO_READ_TARGETS)]
    category: Slug


class PhotoFactsReadResult(WireModel):
    """The pipeline's answer: the observations it kept, plus the records of its calls. A failed
    call is a result with `error`, no observations and its failed record; it never raises."""

    schema_version: str = SCHEMA_VERSION
    observations: Annotated[list[PhotoObservation], Field(max_length=MAX_PHOTO_OBSERVATIONS)] = (
        Field(default_factory=list)
    )
    agent_runs: Annotated[list[AgentRunRecord], Field(max_length=2)] = Field(default_factory=list)
    error: ErrorReport | None = None
