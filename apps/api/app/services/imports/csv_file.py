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

The header is bounded before the csv module splits it (audit FILES-1): its record may take at
most `MAX_HEADER_CHARS` characters of the file and `MAX_HEADER_COLUMNS` columns, else the file
is refused at `file.header`. The A.6 contract needs about 170 names (64, the aliases and
`bullet_1`..`bullet_99`), so neither cap refuses a real header.

A spreadsheet export often ends every line in one or more empty cells (audit FILES-2). Trailing
header cells that are blank are dropped when no data row has a non-blank cell under them; they
name no column. A blank trailing header cell with a value under it stays, and the header check
reports it as an unknown column.
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
MAX_HEADER_COLUMNS: Final = 256
MAX_HEADER_CHARS: Final = 32_768
HEADER_FIELD: Final = "file.header"
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


class _HeaderGuard:
    """The file's lines, refusing once the header record has used more than
    `MAX_HEADER_CHARS` characters: the check runs on each line before the csv module splits it,
    so a huge header never becomes a huge list of cells."""

    def __init__(self, text: str) -> None:
        self._lines = io.StringIO(text, newline="")
        self.in_header = True
        self._used = 0

    def __iter__(self) -> _HeaderGuard:
        return self

    def __next__(self) -> str:
        line = next(self._lines)
        if self.in_header:
            self._used += len(line)
            if self._used > MAX_HEADER_CHARS:
                raise ImportFileError(
                    file_report(
                        f"the header row is longer than {MAX_HEADER_CHARS} characters",
                        HEADER_FIELD,
                    )
                )
        return line


def _records(text: str) -> Iterator[list[str]]:
    """The header record first (bounded, see `_HeaderGuard`), then every data record."""
    lines = _HeaderGuard(text)
    reader = csv.reader(lines, strict=True)
    try:
        header = next(reader, None)
        if header is None:
            return
        if len(header) > MAX_HEADER_COLUMNS:
            raise ImportFileError(
                file_report(f"the header has more than {MAX_HEADER_COLUMNS} columns", HEADER_FIELD)
            )
        lines.in_header = False
        yield header
        yield from reader
    except csv.Error:
        raise ImportFileError(file_report("the file is not well-formed CSV")) from None


def _named_width(header: list[str]) -> int:
    """The header's width without its trailing blank cells."""
    width = len(header)
    while width and not normalize_header(header[width - 1]):
        width -= 1
    return width


def _last_filled(record: list[str], start: int, stop: int) -> int:
    """One past the last non-blank cell of `record[start:stop]`, or `start` when all are blank."""
    for index in range(min(len(record), stop) - 1, start - 1, -1):
        if record[index].strip():
            return index + 1
    return start


def _blank(record: list[str]) -> bool:
    """An empty line, or a line of only separators and spaces (`,,,`): skipped, not a row
    (KNOWN-ISSUES §E, S13b)."""
    return not any(cell.strip() for cell in record)


def parse_upload(data: bytes) -> ParsedFile:
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    records = _records(_text(data))
    header = next(records, None)
    if header is None or not any(cell.strip() for cell in header):
        raise ImportFileError(file_report("the file has no header row", HEADER_FIELD))
    # trailing blank header cells are kept only up to the last one with a value under it
    width = _named_width(header)
    rows = 0
    for record in records:
        if _blank(record):
            continue
        if width < len(header):
            width = _last_filled(record, width, len(header))
        rows += 1
        if rows > MAX_IMPORT_ROWS:
            raise ImportFileError(
                file_report(f"the file has more than {MAX_IMPORT_ROWS} data rows")
            )
    if rows == 0:
        raise ImportFileError(file_report("the file has a header but no data rows"))
    return ParsedFile(columns=header[:width], row_count=rows)


def _column_name(name: str) -> str:
    """The registry name of a header (only checked headers reach the rows), else a fixed word."""
    key = row_key(name)
    return key if key is not None else "a column"


def iter_rows(data: bytes, start_after: int = 0) -> Iterator[CsvRow]:
    records = _records(_text(data))
    header = next(records, None)
    if header is None:
        return
    names = [normalize_header(h) for h in header[: _named_width(header)]]
    for index, record in enumerate(records, start=2):
        if _blank(record) or index <= start_after:
            continue
        if len(names) < len(record) <= len(header) and not any(
            cell.strip() for cell in record[len(names) :]
        ):
            record = record[: len(names)]  # empty cells under the unnamed trailing columns
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
