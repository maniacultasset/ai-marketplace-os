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
    product_id: UUID | None = Field(
        default=None,
        description=(
            "Omit to mint a product id for a product not created yet (SI1): send it back as "
            "`id` with `image_uploads` on `POST /products`."
        ),
    )
    slot: ImageSlot
    content_type: ImageContentType
    bytes: Annotated[int, Field(ge=1, le=MAX_IMAGE_BYTES)]


class UploadUrlOut(WireModel):
    upload_url: Annotated[NonBlankStr, Field(max_length=4096)]
    storage_key: StorageKey
    expires_at: AwareDatetime
    headers: dict[str, str] = Field(default_factory=dict)
    product_id: UUID = Field(description="The product the key belongs to (minted when omitted).")


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


class PhotoMatchOut(WireModel):
    """V1.1 (client feedback item 9): does the photo show this product in its colour. `checking`
    while the check runs; `match`; `mismatch` or `unsure` with a `question` for the seller (keep
    the photo with `POST /images/{image_id}/photo-match/keep`, or delete it); `failed` when it
    could not be checked. At approval an unkept `mismatch` blocks and an unkept `unsure` warns."""

    status: Literal["checking", "match", "mismatch", "unsure", "failed"]
    colour: str | None = Field(
        default=None, description="The colour checked; null for a photo every colour shares."
    )
    product_match: float | None = Field(default=None, ge=0, le=1)
    colour_match: float | None = Field(default=None, ge=0, le=1)
    seen_colour: str | None = Field(default=None, description="The colour the check saw.")
    reason: str | None = None
    question: str | None = Field(
        default=None, description="The seller's question on a `mismatch` or `unsure` photo."
    )
    checked_at: AwareDatetime | None = None
    kept: bool = Field(default=False, description="The seller kept this photo after the question.")
    kept_at: AwareDatetime | None = None
    kept_by_user_id: UUID | None = None


class PhotoMatchKeptOut(WireModel):
    image_id: UUID
    photo_match: PhotoMatchOut
