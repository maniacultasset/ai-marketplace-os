"""Audit FILES-1: the CSV header is bounded before it is parsed."""

from __future__ import annotations

import tracemalloc

import pytest
from app.services.imports.csv_file import (
    MAX_HEADER_CHARS,
    MAX_HEADER_COLUMNS,
    ImportFileError,
    iter_rows,
    parse_upload,
)
from app.services.imports.headers import check_header
from app.services.imports.upload import _file_problem
from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import MAX_ERROR_ITEMS

REQUEST_CAP = 10 * 1024 * 1024


def _refusal(data: bytes) -> str:
    with pytest.raises(ImportFileError) as caught:
        parse_upload(data)
    report = caught.value.report
    assert report.error_code is ErrorCode.IMPORT_FAILED
    assert [e.field for e in report.errors] == ["file.header"]
    return report.errors[0].message


# ---- FILES-1: a header of millions of columns ----


def test_wide_header_is_refused_with_bounded_memory() -> None:
    data = b"a," * 5_199_900 + b"a\nx\n"
    assert len(data) < REQUEST_CAP
    tracemalloc.start()
    try:
        report, rows = _file_problem(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report is not None and rows == 0
    assert report.error_code is ErrorCode.IMPORT_FAILED
    assert len(report.errors) == 1
    # the file itself (10 MiB) and its decoded text are the floor; no per-column cost on top
    assert peak < 8 * len(data), peak


def test_header_over_the_column_cap_is_refused() -> None:
    header = ",".join(f"c{i}" for i in range(MAX_HEADER_COLUMNS + 1))
    message = _refusal(f"{header}\nx\n".encode())
    assert message == f"the header has more than {MAX_HEADER_COLUMNS} columns"


def test_header_at_the_column_cap_reaches_the_header_check() -> None:
    header = ["seller_sku", "product_name", "category"] + [f"bullet_{i}" for i in range(1, 100)]
    header += ["x"] * (MAX_HEADER_COLUMNS - len(header))
    parsed = parse_upload((",".join(header) + "\nA-1,K,kurtas\n").encode())
    assert len(parsed.columns) == MAX_HEADER_COLUMNS
    assert {code for code, _ in check_header(parsed.columns)} == {ErrorCode.HEADER_UNKNOWN_COLUMN}


def test_header_line_over_the_char_cap_is_refused() -> None:
    name = "q" * (MAX_HEADER_CHARS + 1)
    message = _refusal(f"seller_sku,{name}\nx,y\n".encode())
    assert message == f"the header row is longer than {MAX_HEADER_CHARS} characters"


def test_quoted_newlines_count_towards_the_header_char_cap() -> None:
    cell = '"' + "q\n" * (MAX_HEADER_CHARS // 2 + 1) + '"'
    message = _refusal(f"seller_sku,{cell}\nx,y\n".encode())
    assert message == f"the header row is longer than {MAX_HEADER_CHARS} characters"


def test_iter_rows_refuses_the_same_wide_header() -> None:
    with pytest.raises(ImportFileError):
        list(iter_rows(b"a," * (MAX_HEADER_COLUMNS + 1) + b"a\nx\n"))


def test_check_header_keeps_at_most_the_report_cap() -> None:
    problems = check_header(["zz"] * (MAX_ERROR_ITEMS * 3))
    assert len(problems) == MAX_ERROR_ITEMS
