from uuid import UUID

from pydantic import Field

from mkos_schemas._base import WireModel
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import (
    ErrorClass,
    ErrorSource,
    FixAction,
    Marketplace,
    ReportStatus,
    RetryClass,
    Severity,
)


class ErrorItem(WireModel):
    field: str
    error_code: ErrorCode
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool
    source: ErrorSource
    severity: Severity = "error"
    suggestion: str | None = Field(default=None, min_length=1, max_length=1000)
    auto_fixable: bool = False
    error_class: ErrorClass | None = None
    fix_action: FixAction | None = None
    marketplace: Marketplace | None = None
    raw_code: str | None = Field(default=None, max_length=128)
    retry_class: RetryClass | None = None
    rule_id: UUID | None = None
    value_submitted: str | None = None


class ErrorReport(WireModel):
    status: ReportStatus
    schema_version: str = "1.2.0"
    errors: list[ErrorItem] = Field(default_factory=list, max_length=500)
    error_code: ErrorCode | None = None
    listing_id: UUID | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    retryable: bool = False
    attempts_used: int = Field(default=0, ge=0, le=3)
    max_attempts: int = Field(default=3, ge=1, le=3)
