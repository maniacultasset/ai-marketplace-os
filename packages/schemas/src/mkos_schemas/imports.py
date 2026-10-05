"""Bulk import reports."""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .base import SCHEMA_VERSION, WireModel
from .errors import ErrorItem, ErrorReport

MAX_IMPORT_ROWS: Final = 10_000


class ImportRowResult(WireModel):
    row_number: Annotated[int, Field(ge=2)]
    sku: str | None = None
    status: Literal["imported", "failed"]
    product_id: UUID | None = None
    errors: list[ErrorItem] = Field(default_factory=list)

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

    @model_validator(mode="after")
    def _counts(self) -> ImportReport:
        if self.imported + self.failed != self.total_rows:
            raise ValueError("imported + failed must equal total_rows")
        return self
