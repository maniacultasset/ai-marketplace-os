"""Bulk import reports."""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .base import SCHEMA_VERSION, WireModel
from .errors import ErrorItem, ErrorReport

MAX_IMPORT_ROWS: Final = 10_000
MAX_ROW_PHOTOS: Final = 9

PhotoLinkStatus = Literal["queued", "downloading", "saved", "failed", "skipped"]
PhotoLinkView = Literal[
    "front", "back", "close_up", "lifestyle", "size_chart", "packaging_label", "extra"
]
PhotoLinkProblem = Literal[
    "not_found",
    "not_public",
    "not_image",
    "unsupported_type",
    "too_large",
    "too_many_pixels",
    "corrupt",
    "timeout",
    "unreachable",
    "blocked_address",
    "too_many_redirects",
    "server_error",
    "photo_limit",
    "import_cap",
    "removed_by_seller",
    "product_gone",
]


class PhotoLinkResult(WireModel):
    """G2: one photo link from a spreadsheet row and what became of it. The link itself is never
    returned (it may carry a signed token): `position` and `column` say which cell it was."""

    link_id: UUID
    position: Annotated[int, Field(ge=1, le=MAX_ROW_PHOTOS)] = Field(
        description="Which link of the row this is (1 = the first link), for 'Photo N'."
    )
    column: Literal["main_image", "additional_images"]
    status: PhotoLinkStatus = Field(
        description=(
            "`queued` waits to download; `downloading`; `saved` became a product photo "
            "(`image_id`, `view`); `failed` could not be used (`problem`, `message`); `skipped` "
            "was not downloaded (`problem`: the per-file cap, a photo the seller removed, or a "
            "deleted product)."
        )
    )
    view: PhotoLinkView | None = Field(
        default=None,
        description=(
            "The view the saved photo holds now (`extra` for a plain additional photo); null "
            "until saved, and when the saved photo was removed."
        ),
    )
    image_id: UUID | None = None
    colourway_id: UUID | None = Field(
        default=None, description="The colour the photo belongs to on a style split by colour."
    )
    problem: PhotoLinkProblem | None = None
    http_status: Annotated[int, Field(ge=100, le=599)] | None = None
    message: str | None = Field(
        default=None,
        description="The seller sentence for a failed or skipped link (prefix it with Photo N).",
    )
    retryable: bool = Field(
        default=False,
        description="A retry (`POST .../photo-links/retry`) would download this link again.",
    )


class ImportPhotoTotals(WireModel):
    """G2: the photo links of one import, by outcome (`failed` counts skipped links too)."""

    total: Annotated[int, Field(ge=0)]
    waiting: Annotated[int, Field(ge=0)]
    saved: Annotated[int, Field(ge=0)]
    failed: Annotated[int, Field(ge=0)]


class ImportRowResult(WireModel):
    row_number: Annotated[int, Field(ge=2)]
    sku: str | None = None
    status: Literal["imported", "failed"]
    product_id: UUID | None = None
    errors: list[ErrorItem] = Field(default_factory=list)
    photos: Annotated[list[PhotoLinkResult], Field(max_length=MAX_ROW_PHOTOS)] = Field(
        default_factory=list,
        description=(
            "G2: this row's photo links and what became of each (empty while the photo download "
            "is switched off, and for a row whose links another row of the file already named)."
        ),
    )

    @model_validator(mode="after")
    def _failed_needs_errors(self) -> ImportRowResult:
        if self.status == "failed" and not self.errors:
            raise ValueError("failed rows must include at least one error")
        return self


class ImportReport(WireModel):
    schema_version: str = SCHEMA_VERSION
    import_id: UUID
    total_rows: Annotated[int, Field(ge=0)]
    imported: Annotated[int, Field(ge=0)]
    failed: Annotated[int, Field(ge=0)]
    rows: Annotated[list[ImportRowResult], Field(max_length=MAX_IMPORT_ROWS)] = Field(
        default_factory=list
    )
    error: ErrorReport | None = None
    photos: ImportPhotoTotals | None = Field(
        default=None,
        description="G2: the file's photo links by outcome; null when the import named none.",
    )

    @model_validator(mode="after")
    def _counts(self) -> ImportReport:
        if self.imported + self.failed != self.total_rows:
            raise ValueError("imported + failed must equal total_rows")
        return self
