"""AI seam payloads for one image edit (SI7, ADR-0030 proposed).

The backend sends one square JPEG of the seller's main photo; the pipeline answers with one edited
image and the one `AgentRunRecord` of the call. The backend judges, composites and frames the
result itself, so the pipeline never decides what reaches a listing.

`EditTask` has one member: a later member (for example `lifestyle`) is a MAJOR change and waits
for a contract window (docs/RULES.md §11).
"""

from __future__ import annotations

import binascii
from base64 import b64decode
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from .agent import AgentRunRecord
from .base import SCHEMA_VERSION, NonBlankStr, RequestId, WireModel
from .errors import ErrorReport
from .jobs import ContextImage

__all__ = [
    "MAX_EDIT_IMAGE_B64_CHARS",
    "EditTask",
    "ImageEditRequest",
    "ImageEditResult",
]

# About 10 MiB decoded: the backend's image child refuses a larger input anyway (CP-D10).
MAX_EDIT_IMAGE_B64_CHARS: Final = 14_000_000

EditTask = Literal["white_background"]

_PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC: Final = b"\xff\xd8\xff"


def _is_allowed_image(head: bytes) -> bool:
    if head.startswith(_PNG_MAGIC) or head.startswith(_JPEG_MAGIC):
        return True
    return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"


class ImageEditRequest(WireModel):
    """One edit of one product photo. The image is the backend's square JPEG (at most 1024 px)."""

    model_config = WireModel.model_config | ConfigDict(hide_input_in_errors=True)

    schema_version: str = SCHEMA_VERSION
    tenant_id: UUID
    product_id: UUID
    job_id: UUID
    request_id: RequestId
    attempt: Annotated[int, Field(ge=1, le=3)] = 1
    task: EditTask
    image: ContextImage
    category: Annotated[NonBlankStr, Field(max_length=64)]


class ImageEditResult(WireModel):
    """The pipeline's answer: one edited image and one record when `ok`; an error when `failed`."""

    model_config = WireModel.model_config | ConfigDict(hide_input_in_errors=True)

    schema_version: str = SCHEMA_VERSION
    status: Literal["ok", "failed"]
    # Kept out of repr and of validation errors: the bytes never reach a log line.
    image_b64: Annotated[str | None, Field(repr=False)] = None
    mime: Literal["image/png", "image/jpeg", "image/webp"] | None = None
    # The provider name the gateway reports for the call. Free text, not a closed vocabulary.
    served_provider: Annotated[NonBlankStr | None, Field(max_length=64)] = None
    agent_runs: Annotated[list[AgentRunRecord], Field(max_length=1)] = Field(default_factory=list)
    error: ErrorReport | None = None

    @field_validator("image_b64", mode="plain")
    @classmethod
    def _image_base64(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("image_b64 must be a non-empty base64 string")
        if len(value) > MAX_EDIT_IMAGE_B64_CHARS:
            raise ValueError(f"image_b64 exceeds {MAX_EDIT_IMAGE_B64_CHARS} characters")
        try:
            head = b64decode(value[:16], validate=True) if len(value) >= 16 else b""
            decoded = b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("image_b64 is not valid base64") from None
        if not _is_allowed_image(head or decoded[:12]):
            raise ValueError("image_b64 is not a PNG, JPEG or WebP image")
        return value

    @model_validator(mode="after")
    def _status_shape(self) -> ImageEditResult:
        if self.status == "ok":
            if self.image_b64 is None or self.mime is None:
                raise ValueError("an ok result needs image_b64 and mime")
            if len(self.agent_runs) != 1:
                raise ValueError("an ok result carries exactly one agent run")
            if self.error is not None:
                raise ValueError("an ok result carries no error")
        else:
            if self.error is None:
                raise ValueError("a failed result needs an error")
            if self.image_b64 is not None or self.mime is not None:
                raise ValueError("a failed result carries no image")
        return self
