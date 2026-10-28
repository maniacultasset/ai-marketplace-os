from datetime import datetime
from uuid import UUID

from pydantic import Field

from mkos_schemas._base import WireModel
from mkos_schemas.enums import ErrorSource, Marketplace
from mkos_schemas.errors import ErrorReport


class ValidationResult(WireModel):
    listing_id: UUID
    marketplace: Marketplace
    passed: bool
    report: ErrorReport
    source: ErrorSource
    tenant_id: UUID
    created_at: datetime
    attempt: int = Field(default=1, ge=1)
    duration_ms: int = Field(default=0, ge=0)
    listing_version_id: UUID | None = None
    rules_version: int | None = None
    schema_version: str = "1.2.0"
    validation_run_id: UUID | None = None


class ValidationSummary(WireModel):
    listing_id: UUID
    passed: bool
    schema_version: str = "1.2.0"
    listing_version_id: UUID | None = None
    request_id: str | None = None
    stages: list[ValidationResult] = Field(default_factory=list)
    blocking_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
