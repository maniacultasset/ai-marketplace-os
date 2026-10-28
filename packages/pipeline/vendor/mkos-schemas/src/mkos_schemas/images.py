from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from mkos_schemas._base import WireModel
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.listing import ImageRef

ImageCheckName = Literal[
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
ImageIssueCode = Literal[
    "BACKGROUND_OFF",
    "RESOLUTION_LOW",
    "TEXT_DETECTED",
    "WATERMARK",
    "FILL_RATIO_LOW",
    "WRONG_ASPECT",
    "FORMAT_UNSUPPORTED",
    "FILE_TOO_LARGE",
    "CORRUPT",
    "LOGO_PROHIBITED",
]


class ImageCheck(WireModel):
    check: ImageCheckName
    method: Literal["deterministic", "vision"]
    passed: bool


class ImageIssue(WireModel):
    check: ImageCheckName
    code: ImageIssueCode
    suggestion: str = Field(min_length=1, max_length=1000)


class ImageReport(WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    image_id: UUID
    marketplace: Marketplace
    slot: ImageSlot
    pass_: bool = Field(alias="pass")
    processed_at: datetime
    schema_version: str = "1.2.0"
    checks: list[ImageCheck] = Field(default_factory=list)
    issues: list[ImageIssue] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    agent_run_id: UUID | None = None
    model_used: str | None = None


__all__ = ["ImageCheck", "ImageIssue", "ImageRef", "ImageReport"]
