"""S26 tester (KNOWN-ISSUES §E, S13b fix): separator-only rows are skipped consistently.

`parse_upload` (the row cap, the count stored on the import) and `iter_rows` (the rows the job
writes) must agree on which records are data, and the 10,000-row cap counts data rows only.
"""

from __future__ import annotations

import pytest
from app.services.imports.csv_file import ImportFileError, iter_rows, parse_upload
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.imports import MAX_IMPORT_ROWS

HEADER = "seller_sku,product_name,category\n"
BLANKS = ["\n", ",,\n", " , ,\n", ",,,,,,\n", '"","",""\n', "\t,\t,\t\n"]


def _data_row(i: int) -> str:
    return f"S-{i},Kurta {i},kurtas\n"


def test_cap_counts_data_rows_not_separator_rows() -> None:
    body = "".join(_data_row(i) + BLANKS[i % len(BLANKS)] for i in range(MAX_IMPORT_ROWS))
    parsed = parse_upload((HEADER + body).encode())
    assert parsed.row_count == MAX_IMPORT_ROWS


def test_cap_still_refuses_one_more_data_row_among_separators() -> None:
    body = "".join(_data_row(i) + ",,\n" for i in range(MAX_IMPORT_ROWS + 1))
    with pytest.raises(ImportFileError):
        parse_upload((HEADER + body).encode())


@settings(max_examples=200, deadline=None)
@given(
    st.lists(st.one_of(st.sampled_from(BLANKS), st.integers(0, 999).map(_data_row)), max_size=40)
)
def test_parse_count_equals_iter_rows(lines: list[str]) -> None:
    data = (HEADER + "".join(lines)).encode()
    expected = sum(1 for line in lines if line not in BLANKS)
    if expected == 0:
        with pytest.raises(ImportFileError):
            parse_upload(data)
        return
    assert parse_upload(data).row_count == expected
    rows = list(iter_rows(data))
    assert len(rows) == expected
    assert all(r.error is None for r in rows)
    # row numbers count every CSV record from the header, blanks included (resume stays exact)
    numbers = [i + 2 for i, line in enumerate(lines) if line not in BLANKS]
    assert [r.row_number for r in rows] == numbers
