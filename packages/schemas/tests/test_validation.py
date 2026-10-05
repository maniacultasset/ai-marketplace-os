"""ValidationResult and ValidationSummary."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.validation import ValidationResult, ValidationSummary

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_LID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _report(**overrides: object) -> ErrorReport:
    base = dict(status="passed", errors=[], attempts_used=0)
    base.update(overrides)
    return ErrorReport.model_validate(base)


def _result(**overrides: object) -> ValidationResult:
    base = dict(
        tenant_id=_TID,
        listing_id=_LID,
        marketplace=Marketplace.AMAZON,
        source=ErrorSource.SCHEMA,
        passed=True,
        report=_report(),
        created_at=_TS,
    )
    base.update(overrides)
    return ValidationResult.model_validate(base)


def test_t60_validation_source_uses_error_source() -> None:
    for src in ErrorSource:
        _result(source=src)
    for bad in ("rules", "vision", "marketplace_api"):
        with pytest.raises(ValidationError):
            _result(source=bad)


def test_validation_result_report_is_error_report() -> None:
    r = _result()
    assert isinstance(r.report, ErrorReport)


def test_validation_result_passed_matches_report() -> None:
    _result(passed=True, report=_report(status="passed"))
    blocking = ErrorItem.model_validate(
        dict(
            field="content.title",
            error_code=ErrorCode.FIELD_TOO_LONG,
            message="bad",
            source=ErrorSource.RULES_ENGINE,
            retryable=False,
            severity=Severity.ERROR,
        )
    )
    with pytest.raises(ValidationError):
        _result(
            passed=True,
            report=_report(status="failed", errors=[blocking]),
        )


def test_validation_summary_counts_non_negative() -> None:
    ValidationSummary.model_validate(dict(listing_id=_LID, passed=True, blocking_count=0))
    with pytest.raises(ValidationError):
        ValidationSummary.model_validate(
            dict(listing_id=_LID, passed=True, blocking_count=-1)
        )
