"""S26 fix round 1 (KNOWN-ISSUES §E, CSV-import follow-ups): a rows retry keeps the stored error of
a row the parser refused. Such a row is stored with no cells (`raw_row {}`); re-running it from
those cells used to replace its real error with four `REQUIRED_FIELD_MISSING` (invariant 8)."""

from __future__ import annotations

import httpx
import pytest
from mkos_schemas.jobs import JobAccepted

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .imports_support import csv_bytes, headers, import_fixture, imports_db, row, run_job
from .jobs_support import JobWorld, job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]


def _codes(report: dict[str, object] | None) -> list[str]:
    assert report is not None
    errors = report.get("errors")
    assert isinstance(errors, list)
    return [str(e["error_code"]) for e in errors]


def _failed(jw: JobWorld, import_id: object) -> dict[int, dict[str, object]]:
    rows = jw.conn.execute(
        "SELECT row_number, error_report_json FROM import_rows "
        "WHERE import_id = %s AND status = 'failed' ORDER BY row_number",
        [import_id],
    ).fetchall()
    return {int(r[0]): r[1] for r in rows}


async def test_rows_retry_keeps_the_parser_error(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    good = row(1)
    data = csv_bytes([good, row(2, selling_price="abc")])
    data += b"SKU-EXTRA," + b",".join([b"x"] * len(good)) + b"\n"  # more cells than the header
    import_id, job_id = await import_fixture(imports_db, api, data)
    await run_job(job_id)
    assert job_row(imports_db.conn, job_id)["status"] == "partial"
    before = _failed(imports_db, import_id)
    assert _codes(before[4]) == ["VALUE_NOT_ALLOWED"]
    assert 3 in before

    response = await api.post(
        f"{PREFIX}/imports/{import_id}/rows/retry", headers=headers(imports_db)
    )
    assert response.status_code == 202, response.text
    retry_job = JobAccepted.model_validate(response.json()).job_id
    await run_job(retry_job)
    assert job_row(imports_db.conn, retry_job)["status"] == "partial"

    after = _failed(imports_db, import_id)
    assert set(after) == {3, 4}
    assert after[4] == before[4]  # the parser's report, untouched
    assert "REQUIRED_FIELD_MISSING" not in _codes(after[4])
