"""Audit FILES-1 and FILES-2: the CSV header is bounded before it is parsed, and a trailing
spreadsheet-export column with no name and no cells is not an unknown column."""

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


# ---- FILES-2: a trailing empty column from a spreadsheet export ----

TRAILING = (
    b"seller_sku,product_name,category,mrp,selling_price,main_image,\n"
    b"SKU-1,Cotton Kurta,ethnic_wear_kurta_sets,1299,999,https://cdn.example.com/p/1.jpg,\n"
)


def test_trailing_empty_header_column_is_ignored() -> None:
    parsed = parse_upload(TRAILING)
    assert parsed.columns[-1] == "main_image"
    assert check_header(parsed.columns) == []
    got = list(iter_rows(TRAILING))
    assert [r.error for r in got] == [None]
    assert "" not in got[0].cells
    assert got[0].cells["main_image"] == "https://cdn.example.com/p/1.jpg"


def test_several_trailing_empty_columns_and_blank_cells_are_ignored() -> None:
    data = b"seller_sku,product_name,category, ,,\nA-1,K,kurtas, ,,\nA-2,K,kurtas\n"
    parsed = parse_upload(data)
    assert parsed.columns == ["seller_sku", "product_name", "category"]
    assert parsed.row_count == 2
    got = list(iter_rows(data))
    assert [r.error for r in got] == [None, None]
    assert list(got[0].cells) == ["seller_sku", "product_name", "category"]


def test_trailing_unnamed_column_with_a_value_is_still_refused() -> None:
    data = b"seller_sku,product_name,category,\nA-1,K,kurtas,\nA-2,K,kurtas,surprise\n"
    parsed = parse_upload(data)
    assert len(parsed.columns) == 4
    assert [code for code, _ in check_header(parsed.columns)] == [ErrorCode.HEADER_UNKNOWN_COLUMN]


def test_only_the_empty_tail_is_dropped() -> None:
    data = b"seller_sku,product_name,category,,,\nA-1,K,kurtas,,v,\n"
    parsed = parse_upload(data)
    assert len(parsed.columns) == 5
    assert len(check_header(parsed.columns)) == 2


def test_a_row_longer_than_the_full_header_still_fails() -> None:
    data = b"seller_sku,product_name,category,\nA-1,K,kurtas,,,\n"
    assert parse_upload(data).columns == ["seller_sku", "product_name", "category"]
    got = list(iter_rows(data))
    assert got[0].error is not None and got[0].error[0] is ErrorCode.VALUE_NOT_ALLOWED
