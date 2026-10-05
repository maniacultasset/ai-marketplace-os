"""Image upload and report models (owner step S17b)."""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from mkos_schemas.enums import ImageSlot, JobState
from mkos_schemas.images import ImageReport
from pydantic import AwareDatetime, Field

from app.api.v1.schemas.common import StorageKey

MAX_IMAGE_BYTES: Final = 10_485_760
ImageContentType = Literal["image/jpeg", "image/png", "image/webp"]


class UploadUrlRequest(StrictModel):
    product_id: UUID
    slot: ImageSlot
    content_type: ImageContentType
    bytes: Annotated[int, Field(ge=1, le=MAX_IMAGE_BYTES)]


class UploadUrlOut(WireModel):
    upload_url: Annotated[NonBlankStr, Field(max_length=4096)]
    storage_key: StorageKey
    expires_at: AwareDatetime
    headers: dict[str, str] = Field(default_factory=dict)


class ImageAccepted(WireModel):
    image_id: UUID
    job_id: UUID
    status: JobState = JobState.QUEUED
    poll_url: NonBlankStr


class ImageReports(WireModel):
    amazon: ImageReport | None = None
    flipkart: ImageReport | None = None


class ImageReportOut(WireModel):
    image_id: UUID
    compliance_status: Literal["pending", "passed", "failed"]
    reports: ImageReports = Field(default_factory=ImageReports)
