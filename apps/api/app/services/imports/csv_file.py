"""The CSV file parser and its file-level limits (S13 AC 20, Q4; BACKEND-ARCHITECTURE §3.3).

`parse_upload(data)` checks the whole file before anything is stored: strict UTF-8 (a leading
BOM is allowed), no NUL byte, well-formed CSV (`csv` with `strict=True`, so an unterminated
quote is an error), and 1..`MAX_IMPORT_ROWS` data rows after the header. A failure is
`ImportFileError(report)`: one `IMPORT_FAILED` item at `file.row` (source `schema`) whose
message is our own text, never a cell.

`iter_rows(data, start_after=0)` yields every data row of a file `parse_upload` accepted as a
`CsvRow`: `row_number` counts CSV records from the header (row 1), so a quoted newline stays in
one row; `cells` maps each normalised header to its cell, a short row padded with `""`. A row
with more cells than the header, or a cell over `MAX_CELL_CHARS`, carries a row `error`
instead (the row fails; the file goes on). Blank records are skipped but keep their number.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.imports import MAX_IMPORT_ROWS

from app.services.imports.headers import normalize_header, row_key

ROW_FIELD: Final = "file.row"
MAX_CELL_CHARS: Final = 5_000
# The csv module's own per-field cap (default 128 KiB) is raised so a long cell reaches the
# row-level check instead of failing the file; the 10 MiB request cap bounds it anyway.
CSV_FIELD_LIMIT: Final = 16 * 1024 * 1024
csv.field_size_limit(CSV_FIELD_LIMIT)


class ImportFileError(Exception):
    """The whole file is refused; `report` is the one `IMPORT_FAILED` report."""

    def __init__(self, report: ErrorReport) -> None:
        super().__init__("import file refused")
        self.report = report


def file_report(message: str, field_path: str = ROW_FIELD) -> ErrorReport:
    item = ErrorItem(
        field=field_path,
        error_code=ErrorCode.IMPORT_FAILED,
        message=message,
        source=ErrorSource.SCHEMA,
        retryable=False,
    )
    return ErrorReport(
        status="failed", error_code=ErrorCode.IMPORT_FAILED, retryable=False, errors=[item]
    )


@dataclass(frozen=True, slots=True)
class ParsedFile:
    columns: list[str]
    row_count: int


@dataclass(frozen=True, slots=True)
class CsvRow:
    row_number: int
    cells: dict[str, str] = field(default_factory=dict)
    error: tuple[ErrorCode, str] | None = None


def _text(data: bytes) -> str:
    if b"\x00" in data:
        raise ImportFileError(file_report("the file contains a NUL byte"))
    try:
        return data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise ImportFileError(file_report("the file is not valid UTF-8")) from None


def _records(text: str) -> Iterator[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        yield from reader
    except csv.Error:
        raise ImportFileError(file_report("the file is not well-formed CSV")) from None


def parse_upload(data: bytes) -> ParsedFile:
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    records = _records(_text(data))
    header = next(records, None)
    if header is None or not any(cell.strip() for cell in header):
        raise ImportFileError(file_report("the file has no header row", "file.header"))
    rows = 0
    for record in records:
        if not record:
            continue
        rows += 1
        if rows > MAX_IMPORT_ROWS:
            raise ImportFileError(
                file_report(f"the file has more than {MAX_IMPORT_ROWS} data rows")
            )
    if rows == 0:
        raise ImportFileError(file_report("the file has a header but no data rows"))
    return ParsedFile(columns=header, row_count=rows)


def _column_name(name: str) -> str:
    """The registry name of a header (only checked headers reach the rows), else a fixed word."""
    key = row_key(name)
    return key if key is not None else "a column"


def iter_rows(data: bytes, start_after: int = 0) -> Iterator[CsvRow]:
    records = _records(_text(data))
    header = next(records, None)
    if header is None:
        return
    names = [normalize_header(h) for h in header]
    for index, record in enumerate(records, start=2):
        if not record or index <= start_after:
            continue
        if len(record) > len(names):
            yield CsvRow(
                index, error=(ErrorCode.VALUE_NOT_ALLOWED, "the row has more cells than the header")
            )
            continue
        padded = record + [""] * (len(names) - len(record))
        too_long = next(
            (name for name, cell in zip(names, padded, strict=True) if len(cell) > MAX_CELL_CHARS),
            None,
        )
        if too_long is not None:
            yield CsvRow(
                index,
                error=(
                    ErrorCode.FIELD_TOO_LONG,
                    f"{_column_name(too_long)} is longer than {MAX_CELL_CHARS} characters",
                ),
            )
            continue
        yield CsvRow(index, cells=dict(zip(names, padded, strict=True)))
