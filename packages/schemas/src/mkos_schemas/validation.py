"""Validation run results."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .base import SCHEMA_VERSION, WireModel
from .enums import ErrorSource, Marketplace
from .errors import ErrorReport

MAX_VALIDATION_STAGES = 20


class ValidationResult(WireModel):
    schema_version: str = SCHEMA_VERSION
    validation_run_id: UUID | None = None
    tenant_id: UUID
    listing_id: UUID
    listing_version_id: UUID | None = None
    marketplace: Marketplace
    source: ErrorSource
    passed: bool
    attempt: Annotated[int, Field(ge=1)] = 1
    rules_version: int | None = None
    duration_ms: Annotated[int, Field(ge=0)] = 0
    report: ErrorReport
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _passed_matches_report(self) -> ValidationResult:
        if self.passed != self.report.passed:
            raise ValueError("passed must equal report.passed")
        return self


class ValidationSummary(WireModel):
    schema_version: str = SCHEMA_VERSION
    listing_id: UUID
    listing_version_id: UUID | None = None
    passed: bool
    stages: list[ValidationResult] = Field(default_factory=list)
    blocking_count: Annotated[int, Field(ge=0)] = 0
    warning_count: Annotated[int, Field(ge=0)] = 0
    request_id: str | None = None
