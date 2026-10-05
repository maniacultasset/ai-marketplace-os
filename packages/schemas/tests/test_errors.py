"""ErrorItem and ErrorReport."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, FixAction, Marketplace, RetryClass, Severity
from mkos_schemas.errors import MAX_ERROR_ITEMS, VALUE_SUBMITTED_MAX, ErrorItem, ErrorReport


def _item(**overrides: object) -> ErrorItem:
    base = dict(
        field="content.title",
        error_code=ErrorCode.FIELD_TOO_LONG,
        message="Title too long",
        source=ErrorSource.RULES_ENGINE,
        retryable=True,
    )
    base.update(overrides)
    return ErrorItem.model_validate(base)


def test_error_item_truncates() -> None:
    exact = "x" * 600
    assert _item(value_submitted=exact).value_submitted == exact
    long601 = "y" * 601
    assert len(_item(value_submitted=long601).value_submitted or "") == 600
    long10k = "z" * 10_000
    assert len(_item(value_submitted=long10k).value_submitted or "") == 600


def test_error_item_truncates_by_code_point() -> None:
    emoji = "😀" * 601
    assert len(_item(value_submitted=emoji).value_submitted or "") == 600
    devanagari = "क" * 601
    assert len(_item(value_submitted=devanagari).value_submitted or "") == 600


def test_error_item_value_submitted_strips_nul() -> None:
    assert _item(value_submitted="a\x00b").value_submitted == "ab"


@given(st.text(min_size=0, max_size=2000))
@settings(max_examples=50)
def test_error_item_property_truncation(raw: str) -> None:
    item = _item(value_submitted=raw)
    expected = raw.replace("\x00", "")[:VALUE_SUBMITTED_MAX]
    assert item.value_submitted == expected


def test_error_item_field_must_be_legal_path() -> None:
    _item(field="content.title")
    with pytest.raises(ValidationError):
        _item(field="title")


def test_error_item_rejects_rule_wildcards() -> None:
    with pytest.raises(ValidationError):
        _item(field="*")
    with pytest.raises(ValidationError):
        _item(field="content.bullets[*]")


def test_error_item_source_vocabulary() -> None:
    for bad in ("api", "rules"):
        with pytest.raises(ValidationError):
            _item(source=bad)


def test_error_item_classification_fields_optional() -> None:
    obj = _item()
    assert obj.rule_id is None
    assert obj.raw_code is None
    assert obj.error_class is None
    assert obj.retry_class is None
    assert obj.auto_fixable is False
    assert obj.fix_action is None


def test_error_item_retryable_matches_retry_class() -> None:
    rid = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    _item(
        retry_class=RetryClass.AUTO_RETRY,
        retryable=True,
        rule_id=rid,
    )
    with pytest.raises(ValidationError):
        _item(retry_class=RetryClass.AUTO_RETRY, retryable=False)
    _item(retry_class=RetryClass.USER_ACTION, retryable=False)


def test_error_item_auto_fixable_iff_fix_action() -> None:
    _item(auto_fixable=True, fix_action=FixAction.REFORMAT)
    with pytest.raises(ValidationError):
        _item(auto_fixable=True, fix_action=None)
    with pytest.raises(ValidationError):
        _item(auto_fixable=False, fix_action=FixAction.REFORMAT)


def test_error_item_raw_code_max_128() -> None:
    _item(raw_code="x" * 128)
    with pytest.raises(ValidationError):
        _item(raw_code="y" * 129)


def test_error_report_attempts_capped_at_3() -> None:
    ErrorReport.model_validate(
        dict(status="failed", attempts_used=3, max_attempts=3, errors=[])
    )
    with pytest.raises(ValidationError):
        ErrorReport.model_validate(
            dict(status="failed", attempts_used=4, max_attempts=3, errors=[])
        )
    with pytest.raises(ValidationError):
        ErrorReport.model_validate(
            dict(status="failed", attempts_used=0, max_attempts=0, errors=[])
        )


def test_error_report_blocking_and_passed() -> None:
    err = _item(severity=Severity.ERROR)
    warn = _item(severity=Severity.WARNING, field="content.bullets[0]")
    report = ErrorReport.model_validate(
        dict(status="failed", errors=[err, warn], attempts_used=1)
    )
    assert len(report.blocking) == 1
    assert report.blocking[0].severity is Severity.ERROR
    assert report.passed is False
    ok = ErrorReport.model_validate(
        dict(status="passed", errors=[warn], attempts_used=0)
    )
    assert ok.passed is True


def test_error_report_properties_not_serialized() -> None:
    report = ErrorReport.model_validate(
        dict(
            status="failed",
            errors=[_item()],
            attempts_used=1,
            blocking=[],
            passed=True,
        )
    )
    dumped = report.model_dump()
    assert "blocking" not in dumped
    assert "passed" not in dumped
    schema = ErrorReport.model_json_schema(mode="serialization")
    assert "blocking" not in schema.get("properties", {})
    assert "passed" not in schema.get("properties", {})


SPEC_VERBATIM: dict[str, Any] = {
    "status": "failed",
    "error_code": "VALIDATION_FAILED",
    "retryable": True,
    "attempts_used": 1,
    "max_attempts": 3,
    "errors": [
        {
            "field": "title",
            "error_code": "TITLE_TOO_LONG",
            "message": "Title is 221 chars; Amazon limit is 200",
            "value_submitted": "Organic Cotton Kurta Set... (221 chars)",
            "suggestion": "Shorten to <= 200 chars; move detail to bullets",
            "source": "rules_engine",
            "retryable": True,
            "marketplace": "amazon",
        }
    ],
}

SPEC_MAPPED = {
    **SPEC_VERBATIM,
    "errors": [
        {
            **SPEC_VERBATIM["errors"][0],
            "field": "content.title",
            "error_code": "FIELD_TOO_LONG",
        }
    ],
}


def test_spec_121_example_maps() -> None:
    with pytest.raises(ValidationError):
        ErrorReport.model_validate(SPEC_VERBATIM)
    ErrorReport.model_validate(SPEC_MAPPED)


def test_no_api_error_detail_dict() -> None:
    assert "api_error_detail" not in ErrorItem.model_fields
    dumped = _item().model_dump()
    assert "api_error_detail" not in dumped
    schema = ErrorItem.model_json_schema()
    assert "api_error_detail" not in schema.get("properties", {})


def test_error_item_suggestion_max_1000() -> None:
    _item(suggestion="s" * 1000)
    with pytest.raises(ValidationError):
        _item(suggestion="t" * 1001)


def test_error_report_errors_list_cap() -> None:
    one = _item()
    ErrorReport.model_validate(
        dict(status="failed", errors=[one] * MAX_ERROR_ITEMS, attempts_used=0)
    )
    with pytest.raises(ValidationError):
        ErrorReport.model_validate(
            dict(status="failed", errors=[one] * (MAX_ERROR_ITEMS + 1), attempts_used=0)
        )
