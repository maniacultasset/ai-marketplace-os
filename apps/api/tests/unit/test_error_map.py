"""S20 T3 (AC4, AC14): the pure marketplace error mapper (DATABASE.md:546-557)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, FixAction, RetryClass, Severity
from mkos_schemas.errors import ErrorReport

MODULE = Path(__file__).resolve().parents[2] / "app" / "adapters" / "error_map.py"
CANARY = "CANARYzq9-0123456789abcdef"


def _row(**over: Any):  # type: ignore[no-untyped-def]
    from app.adapters.error_map import ErrorMapRow

    fields: dict[str, Any] = {
        "marketplace": "amazon",
        "match_on": "code",
        "raw_value": "90220",
        "version": 1,
        "error_code": ErrorCode.MISSING_ATTRIBUTE,
        "error_class": ErrorClass.MISSING_DATA,
        "retry_class": RetryClass.USER_ACTION,
        "severity": Severity.ERROR,
        "auto_fixable": False,
        "fix_action": None,
        "field_hint": None,
        "plain_text": "{field} is required on {marketplace}.",
        "seller_action": "Add the missing value and resubmit.",
        "enabled": True,
    }
    fields.update(over)
    return ErrorMapRow(**fields)


def _mapper(*rows: Any):  # type: ignore[no-untyped-def]
    from app.adapters.error_map import ErrorMapper

    return ErrorMapper(rows)


def test_error_map_lookup_code_before_category() -> None:
    by_code = _row(raw_value="90220")
    by_category = _row(match_on="category", raw_value="MISSING_ATTRIBUTE", version=5)
    found = _mapper(by_category, by_code).lookup("90220", ["MISSING_ATTRIBUTE"])
    assert found is by_code


def test_error_map_lookup_highest_enabled_version() -> None:
    v1, v2, v3 = _row(version=1), _row(version=2), _row(version=3, enabled=False)
    assert _mapper(v1, v3, v2).lookup("90220", []) is v2


def test_error_map_lookup_disabled_ignored() -> None:
    assert _mapper(_row(enabled=False)).lookup("90220", []) is None


def test_error_map_lookup_other_marketplace_ignored() -> None:
    with pytest.raises(ValueError, match="one marketplace"):
        _mapper(_row(), _row(marketplace="flipkart"))


def test_error_map_lookup_category_fallback() -> None:
    row = _row(match_on="category", raw_value="INVALID_ATTRIBUTE")
    assert _mapper(row).lookup("12345", ["OTHER", "INVALID_ATTRIBUTE"]) is row


def test_error_map_lookup_field_hint_when_no_attribute() -> None:
    mapper = _mapper(_row(field_hint="images[0]"))
    item = mapper.to_item(raw_code="90220", raw_message="Image missing", field=None)
    assert item.field == "images[0]"
    no_hint = _mapper(_row()).to_item(raw_code="90220", raw_message="x", field=None)
    assert no_hint.field == "listing"
    named = mapper.to_item(raw_code="90220", raw_message="x", field="content.title")
    assert named.field == "content.title"


def test_mapped_row_message_and_suggestion() -> None:
    item = _mapper(_row()).to_item(
        raw_code="90220", raw_message="'brand' is required.", field="attributes.brand"
    )
    assert item.message == (
        "attributes.brand is required on amazon. Marketplace message: 'brand' is required."
    )
    assert item.suggestion == "Add the missing value and resubmit."
    assert (item.error_code, item.error_class, item.retry_class) == (
        ErrorCode.MISSING_ATTRIBUTE,
        ErrorClass.MISSING_DATA,
        RetryClass.USER_ACTION,
    )
    assert item.source is ErrorSource.AMAZON_ADAPTER and item.marketplace == "amazon"
    assert item.raw_code == "90220"


def test_amazon_unknown_error() -> None:
    mapper = _mapper(_row())
    item = mapper.to_item(raw_code="99999", raw_message="Something odd", field="content.title")
    assert item.error_code is ErrorCode.MARKETPLACE_UNKNOWN_ERROR
    assert item.error_class is ErrorClass.UNKNOWN
    assert item.retry_class is RetryClass.USER_ACTION and item.retryable is False
    assert item.message == "Something odd" and item.raw_code == "99999"
    report = ErrorReport(status="failed", error_code=item.error_code, errors=[item])
    assert ErrorReport.model_validate_json(report.model_dump_json()) == report

    long = mapper.to_item(raw_code="X" * 300, raw_message="m" * 1500, field=None)
    assert len(long.message) == 1000 and long.raw_code == "X" * 128

    leaked = mapper.to_item(
        raw_code="99999",
        raw_message=f"token {CANARY} and Atza|IwEBIExampleLwaToken rejected",
        field=None,
        secrets=(CANARY,),
    )
    assert CANARY not in leaked.message and "Atza|" not in leaked.message
    assert "[REDACTED]" in leaked.message

    empty = mapper.to_item(raw_code="99999", raw_message="", field=None)
    assert empty.message == "Marketplace error"
    blank = mapper.to_item(raw_code="", raw_message="   ", field=None)
    assert blank.message == "Marketplace error" and blank.raw_code is None


def test_plain_text_render_three_placeholders() -> None:
    from app.adapters.error_map import render_plain_text

    out = render_plain_text(
        "{value} is not allowed for {field} on {marketplace}; {value} again.",
        value="XXL",
        field="attributes.size",
        marketplace="amazon",
    )
    assert out == "XXL is not allowed for attributes.size on amazon; XXL again."


def test_plain_text_render_format_spec_literal() -> None:
    from app.adapters.error_map import render_plain_text

    template = "{0.__class__} {value!r} {field:>10} {value}"
    out = render_plain_text(template, value="v", field="f", marketplace="m")
    assert out == "{0.__class__} {value!r} {field:>10} v"


def test_plain_text_render_unicode_value() -> None:
    from app.adapters.error_map import render_plain_text

    out = render_plain_text("{value} ok", value="नीला {field}", field="F", marketplace="m")
    assert out == "नीला {field} ok"  # a value is never re-rendered


def test_mapped_fix_action_not_executed() -> None:
    row = _row(auto_fixable=True, fix_action=FixAction.REMAP_VALUE, plain_text="{value} remap")
    item = _mapper(row).to_item(
        raw_code="90220", raw_message="bad", field="attributes.color", value="navy blue"
    )
    assert item.auto_fixable is True and item.fix_action is FixAction.REMAP_VALUE
    assert item.value_submitted == "navy blue"  # the value is reported, never changed


def test_warning_row_severity() -> None:
    row = _row(severity=Severity.WARNING)
    assert _mapper(row).to_item(raw_code="90220", raw_message="w", field=None).severity == (
        Severity.WARNING
    )


def test_error_map_imports_no_io() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert not names & {"httpx", "socket", "sqlalchemy", "asyncio"}, names
