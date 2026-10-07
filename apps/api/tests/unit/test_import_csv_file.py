"""S13 B3 (AC 20; Q4): the CSV file parser and its file-level limits."""

from __future__ import annotations

import pytest
from app.services.imports.csv_file import (
    MAX_CELL_CHARS,
    CsvRow,
    ImportFileError,
    iter_rows,
    parse_upload,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource

HEADER = "seller_sku,product_name,category\n"


def refused(data: bytes) -> ImportFileError:
    with pytest.raises(ImportFileError) as caught:
        parse_upload(data)
    report = caught.value.report
    assert report.error_code is ErrorCode.IMPORT_FAILED
    assert len(report.errors) == 1
    assert report.errors[0].source is ErrorSource.SCHEMA
    assert report.errors[0].value_submitted is None
    return caught.value


def rows(data: bytes, start_after: int = 0) -> list[CsvRow]:
    return list(iter_rows(data, start_after))


def test_csv_non_utf8_rejected() -> None:
    error = refused((HEADER + "A-1,Kurt\xe4,kurtas\n").encode("latin-1"))
    assert error.report.errors[0].field == "file.row"
    assert "UTF-8" in error.report.errors[0].message


def test_csv_bom_ok() -> None:
    data = ("﻿" + HEADER + "A-1,Kurta,kurtas\n").encode()
    assert parse_upload(data).columns == ["seller_sku", "product_name", "category"]
    assert rows(data)[0].cells["seller_sku"] == "A-1"


def test_csv_crlf_and_lf() -> None:
    crlf = b"seller_sku,product_name,category\r\nA-1,Kurta,kurtas\r\nA-2,Shirt,shirts\r\n"
    lf = crlf.replace(b"\r\n", b"\n")
    assert parse_upload(crlf).row_count == parse_upload(lf).row_count == 2
    assert [r.cells for r in rows(crlf)] == [r.cells for r in rows(lf)]


def test_csv_quoted_newline_one_row() -> None:
    data = (HEADER + 'A-1,"Kurta\nSet",kurtas\nA-2,Shirt,shirts\n').encode()
    assert parse_upload(data).row_count == 2
    got = rows(data)
    assert [r.row_number for r in got] == [2, 3]
    assert got[0].cells["product_name"] == "Kurta\nSet"


def test_csv_10000_rows_ok() -> None:
    body = "".join(f"A-{n},Kurta {n},kurtas\n" for n in range(10_000))
    assert parse_upload((HEADER + body).encode()).row_count == 10_000


def test_csv_10001_rejected_file_row() -> None:
    body = "".join(f"A-{n},Kurta {n},kurtas\n" for n in range(10_001))
    error = refused((HEADER + body).encode())
    assert error.report.errors[0].field == "file.row"
    assert "10000" in error.report.errors[0].message


def test_csv_zero_rows_rejected() -> None:
    error = refused(HEADER.encode())
    assert error.report.errors[0].field == "file.row"
    refused(HEADER.encode() + b"\n\n")
    assert refused(b"").report.errors[0].field == "file.header"


def test_csv_nul_rejected() -> None:
    refused((HEADER + "A-1,Ku\x00rta,kurtas\n").encode())


def test_csv_unterminated_quote_rejected() -> None:
    refused((HEADER + 'A-1,"Kurta,kurtas\n').encode())
    with pytest.raises(ImportFileError):
        rows((HEADER + 'A-1,"Kurta,kurtas\n').encode())


def test_csv_short_row_padded() -> None:
    got = rows((HEADER + "A-1,Kurta\n").encode())
    assert got[0].cells == {"seller_sku": "A-1", "product_name": "Kurta", "category": ""}
    assert got[0].error is None


def test_csv_long_row_error() -> None:
    got = rows((HEADER + "A-1,Kurta,kurtas,extra\nA-2,Shirt,shirts\n").encode())
    assert got[0].error is not None and got[0].error[0] is ErrorCode.VALUE_NOT_ALLOWED
    assert got[0].cells == {}
    assert got[1].error is None


def test_csv_cell_over_cap_error() -> None:
    canary = "Z" * (MAX_CELL_CHARS + 1)
    got = rows((HEADER + f"A-1,{canary},kurtas\n").encode())
    assert got[0].error == (ErrorCode.FIELD_TOO_LONG, "product_name is longer than 5000 characters")
    assert canary not in got[0].error[1]
    ok = rows((HEADER + f"A-1,{'Z' * MAX_CELL_CHARS},kurtas\n").encode())
    assert ok[0].error is None


def test_csv_start_after_skips() -> None:
    data = (HEADER + "A-1,K,kurtas\n\nA-2,K,kurtas\nA-3,K,kurtas\n").encode()
    assert [r.row_number for r in rows(data)] == [2, 4, 5]
    assert [r.row_number for r in rows(data, start_after=4)] == [5]


@settings(max_examples=200)
@given(st.binary(max_size=400) | st.text(max_size=300).map(lambda t: (HEADER + t).encode()))
def test_iter_rows_never_raises_unexpected(data: bytes) -> None:
    try:
        parse_upload(data)
    except ImportFileError:
        return
    for row in iter_rows(data):
        assert row.row_number >= 2
        assert (row.error is None) or row.cells == {}
