"""Unified ErrorReport shape."""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import SCHEMA_VERSION, NonBlankStr, RequestId, WireModel, walk_seam_dicts_and_text
from .codes import ErrorCode
from .enums import ErrorClass, ErrorSource, FixAction, Marketplace, RetryClass, Severity
from .paths import is_valid_field_path

VALUE_SUBMITTED_MAX: Final = 600
MAX_ERROR_ITEMS: Final = 500


def _truncate_value_submitted(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "")
    return text[:VALUE_SUBMITTED_MAX]


class ErrorItem(WireModel):
    field: str
    error_code: ErrorCode
    severity: Severity = Severity.ERROR
    message: Annotated[NonBlankStr, Field(max_length=1000)]
    value_submitted: str | None = None
    suggestion: Annotated[NonBlankStr | None, Field(default=None, max_length=1000)] = None
    source: ErrorSource
    retryable: bool
    marketplace: Marketplace | None = None
    rule_id: UUID | None = None
    raw_code: Annotated[str | None, Field(default=None, max_length=128)] = None
    error_class: ErrorClass | None = None
    retry_class: RetryClass | None = None
    auto_fixable: bool = False
    fix_action: FixAction | None = None

    @field_validator("field")
    @classmethod
    def _path(cls, value: str) -> str:
        if not is_valid_field_path(value):
            raise ValueError(f"{value!r} is not a legal field path")
        return value

    @field_validator("value_submitted", mode="before")
    @classmethod
    def _value_submitted(cls, value: object) -> str | None:
        return _truncate_value_submitted(value)

    @model_validator(mode="after")
    def _classification(self) -> ErrorItem:
        if self.retry_class is not None:
            expected = self.retry_class in {RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT}
            if self.retryable != expected:
                raise ValueError("retryable must match retry_class")
        if self.auto_fixable != (self.fix_action is not None):
            raise ValueError("auto_fixable must match fix_action")
        walk_seam_dicts_and_text(self)
        return self


class ErrorReport(WireModel):
    schema_version: str = SCHEMA_VERSION
    status: Literal["passed", "failed", "partial"]
    error_code: ErrorCode | None = None
    retryable: bool = False
    attempts_used: Annotated[int, Field(ge=0, le=3)] = 0
    max_attempts: Annotated[int, Field(ge=1, le=3)] = 3
    errors: Annotated[list[ErrorItem], Field(max_length=MAX_ERROR_ITEMS)] = Field(
        default_factory=list
    )
    request_id: RequestId | None = None
    listing_id: UUID | None = None

    @property
    def blocking(self) -> list[ErrorItem]:
        return [e for e in self.errors if e.severity is Severity.ERROR]

    @property
    def passed(self) -> bool:
        return not self.blocking

    @model_validator(mode="wrap")
    @classmethod
    def _drop_computed(cls, value: object, handler):  # type: ignore[no-untyped-def]
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in {"blocking", "passed"}}
        return handler(value)
