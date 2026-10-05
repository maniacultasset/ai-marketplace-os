"""ImportReport and row results."""

from __future__ import annotations

from uuid import UUID

import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.errors import ErrorItem
from mkos_schemas.imports import MAX_IMPORT_ROWS, ImportReport, ImportRowResult
from pydantic import ValidationError

_IID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _err(**overrides: object) -> ErrorItem:
    base = {
        "field": "file.header",
        "error_code": ErrorCode.HEADER_MISSING_COLUMN,
        "message": "missing col",
        "source": ErrorSource.SCHEMA,
        "retryable": False,
    }
    base.update(overrides)
    return ErrorItem.model_validate(base)


def test_import_report_fields() -> None:
    names = set(ImportReport.model_fields)
    assert "import_id" in names
    assert "batch_id" not in names


def test_import_report_counts_consistent() -> None:
    ImportReport.model_validate(
        {"import_id": _IID, "total_rows": 3, "imported": 2, "failed": 1, "rows": []}
    )
    with pytest.raises(ValidationError):
        ImportReport.model_validate(
            {"import_id": _IID, "total_rows": 3, "imported": 2, "failed": 2, "rows": []}
        )


def test_import_row_failed_needs_error() -> None:
    ImportRowResult.model_validate(
        {
            "row_number": 2,
            "status": "failed",
            "errors": [
                _err(source=ErrorSource.SCHEMA),
                _err(source=ErrorSource.RULES_ENGINE),
            ],
        }
    )
    with pytest.raises(ValidationError):
        ImportRowResult.model_validate({"row_number": 2, "status": "failed", "errors": []})


def test_import_row_number_starts_at_2() -> None:
    ImportRowResult.model_validate({"row_number": 2, "status": "imported"})
    with pytest.raises(ValidationError):
        ImportRowResult.model_validate({"row_number": 1, "status": "imported"})


def test_import_report_header_error_uses_file_header() -> None:
    report = ImportReport.model_validate(
        {
            "import_id": _IID,
            "total_rows": 0,
            "imported": 0,
            "failed": 0,
            "error": {
                "status": "failed",
                "errors": [_err(field="file.header")],
            },
        }
    )
    assert report.error is not None
    assert report.error.errors[0].field == "file.header"
    assert report.error.errors[0].error_code is ErrorCode.HEADER_MISSING_COLUMN


def test_import_report_rows_cap() -> None:
    row = {"row_number": 2, "status": "imported"}
    ImportReport.model_validate(
        {
            "import_id": _IID,
            "total_rows": MAX_IMPORT_ROWS,
            "imported": MAX_IMPORT_ROWS,
            "failed": 0,
            "rows": [row] * MAX_IMPORT_ROWS,
        }
    )
    with pytest.raises(ValidationError):
        ImportReport.model_validate(
            {
                "import_id": _IID,
                "total_rows": MAX_IMPORT_ROWS + 1,
                "imported": MAX_IMPORT_ROWS + 1,
                "failed": 0,
                "rows": [row] * (MAX_IMPORT_ROWS + 1),
            }
        )
