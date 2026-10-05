"""Image compliance reports."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from .base import SCHEMA_VERSION, NonBlankStr, WireModel
from .codes import ImageIssueCode
from .enums import ImageSlot, Marketplace

CheckName = Literal[
    "format",
    "corruption",
    "resolution",
    "aspect_ratio",
    "file_size",
    "exif_orientation",
    "background",
    "fill_ratio",
    "text_overlay",
    "watermark",
    "logo",
    "product_visibility",
]


class ImageCheck(WireModel):
    check: CheckName
    method: Literal["deterministic", "vision"]
    passed: bool


class ImageIssue(WireModel):
    code: ImageIssueCode
    check: CheckName
    suggestion: Annotated[NonBlankStr, Field(max_length=1000)]


class ImageReport(WireModel):
    schema_version: str = SCHEMA_VERSION
    image_id: UUID
    marketplace: Marketplace
    slot: ImageSlot
    pass_: bool = Field(alias="pass")
    checks: list[ImageCheck] = Field(default_factory=list)
    issues: list[ImageIssue] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    model_used: str | None = None
    agent_run_id: UUID | None = None
    processed_at: AwareDatetime
