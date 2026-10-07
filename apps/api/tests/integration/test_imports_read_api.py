"""S13 B13 (AC 21; Q8): `GET /api/v1/imports/{import_id}` and `/rows`, as app_rw: counts that
match the rows, the in-flight mapping, keyset pages on the row number, the status filter, no
`raw_row`, and the case matrix."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.services.imports import runner
from mkos_schemas.imports import ImportReport, ImportRowResult

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .imports_support import fixture_bytes, import_fixture, imports_db, run_job
from .jobs_support import JobWorld, as_member, bad_token

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]
FIXTURE = "products_50_5_bad.csv"


def url(import_id: uuid.UUID | str, rows: bool = False) -> str:
    return f"{PREFIX}/imports/{import_id}" + ("/rows" if rows else "")


async def done_import(jw: JobWorld, api: httpx.AsyncClient, slug: str = "acme") -> uuid.UUID:
    import_id, job_id = await import_fixture(
        jw, api, fixture_bytes(FIXTURE), email=f"operator@{slug}.test"
    )
    await run_job(job_id)
    return import_id


def me(jw: JobWorld, role: str = "reviewer", slug: str = "acme") -> dict[str, str]:
    return as_member(jw, f"{role}@{slug}.test")


# ---- imports_get ----


@pytest.mark.endpoint("imports_get", case="success")
@pytest.mark.parametrize("role", ["owner", "operator", "reviewer", "admin"])
async def test_imports_get_success(imports_db: JobWorld, api: httpx.AsyncClient, role: str) -> None:
    import_id = await done_import(imports_db, api)
    response = await api.get(url(import_id), headers=me(imports_db, role))
    assert response.status_code == 200, response.text
    report = ImportReport.model_validate(response.json())
    assert (report.import_id, report.total_rows, report.imported, report.failed) == (
        import_id, 50, 45, 5,
    )  # fmt: skip
    assert report.rows == [] and report.error is None


@pytest.mark.endpoint("imports_get", case="validation")
async def test_imports_get_bad_uuid(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.get(url("not-a-uuid"), headers=me(imports_db))
    assert response.status_code == 422


@pytest.mark.endpoint("imports_get", case="missing_auth")
async def test_imports_get_missing_auth(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(url(uuid.uuid4()))).status_code == 401


@pytest.mark.endpoint("imports_get", case="invalid_token", variant="expired")
@pytest.mark.endpoint("imports_get", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("imports_get", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_imports_get_invalid_token(
    imports_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    response = await api.get(url(uuid.uuid4()), headers=bad_token(imports_db, variant))
    assert response.status_code == 401


@pytest.mark.endpoint("imports_get", case="wrong_tenant")
async def test_imports_get_wrong_tenant(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    theirs = await done_import(imports_db, api, "bharat")
    response = await api.get(url(theirs), headers=me(imports_db))
    missing = await api.get(url(uuid.uuid4()), headers=me(imports_db))
    assert response.status_code == missing.status_code == 404
    assert response.json()["error"]["message"] == missing.json()["error"]["message"]


@pytest.mark.endpoint("imports_get", case="not_found")
async def test_imports_get_not_found(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.get(url(uuid.uuid4()), headers=me(imports_db))
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


# ---- imports_rows ----


@pytest.mark.endpoint("imports_rows", case="success")
@pytest.mark.parametrize("role", ["owner", "operator", "reviewer", "admin"])
async def test_imports_rows_success(
    imports_db: JobWorld, api: httpx.AsyncClient, role: str
) -> None:
    import_id = await done_import(imports_db, api)
    response = await api.get(
        url(import_id, rows=True), params={"status": "failed"}, headers=me(imports_db, role)
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [r["row_number"] for r in data] == [8, 15, 22, 35, 43]
    for item in data:
        result = ImportRowResult.model_validate(item)
        assert result.status == "failed" and result.errors


@pytest.mark.endpoint("imports_rows", case="validation")
@pytest.mark.parametrize(
    "params", [{"status": "bogus"}, {"limit": "0"}, {"limit": "201"}, {"cursor": "!!bad!!"}]
)
async def test_imports_rows_validation(
    imports_db: JobWorld, api: httpx.AsyncClient, params: dict[str, str]
) -> None:
    import_id = await done_import(imports_db, api)
    response = await api.get(url(import_id, rows=True), params=params, headers=me(imports_db))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


async def test_imports_rows_cursor_of_another_import_422(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    first = await done_import(imports_db, api)
    second = await done_import(imports_db, api)
    page = (
        await api.get(url(first, rows=True), params={"limit": "2"}, headers=me(imports_db))
    ).json()
    cursor = page["page"]["next_cursor"]
    response = await api.get(
        url(second, rows=True), params={"cursor": cursor}, headers=me(imports_db)
    )
    assert response.status_code == 422


@pytest.mark.endpoint("imports_rows", case="missing_auth")
async def test_imports_rows_missing_auth(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(url(uuid.uuid4(), rows=True))).status_code == 401


@pytest.mark.endpoint("imports_rows", case="invalid_token", variant="expired")
@pytest.mark.endpoint("imports_rows", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("imports_rows", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_imports_rows_invalid_token(
    imports_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    response = await api.get(url(uuid.uuid4(), rows=True), headers=bad_token(imports_db, variant))
    assert response.status_code == 401


@pytest.mark.endpoint("imports_rows", case="wrong_tenant")
async def test_imports_rows_wrong_tenant(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    theirs = await done_import(imports_db, api, "bharat")
    response = await api.get(url(theirs, rows=True), headers=me(imports_db))
    assert response.status_code == 404


@pytest.mark.endpoint("imports_rows", case="not_found")
async def test_imports_rows_not_found(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(url(uuid.uuid4(), rows=True), headers=me(imports_db))).status_code == 404


# ---- the named cases ----


async def test_imports_read_counts_match(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id = await done_import(imports_db, api)
    report = (await api.get(url(import_id), headers=me(imports_db))).json()
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params = {"limit": "200"} | ({"cursor": cursor} if cursor else {})
        page = (
            await api.get(url(import_id, rows=True), params=params, headers=me(imports_db))
        ).json()
        rows += page["data"]
        cursor = page["page"]["next_cursor"]
        if not page["page"]["has_more"]:
            break
    assert len(rows) == report["total_rows"] == 50
    assert sum(r["status"] == "imported" for r in rows) == report["imported"]
    assert sum(r["status"] == "failed" for r in rows) == report["failed"]


async def test_imports_rows_keyset_pages(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id = await done_import(imports_db, api)
    seen: list[int] = []
    cursor: str | None = None
    for _ in range(20):
        params = {"limit": "7"} | ({"cursor": cursor} if cursor else {})
        page = (
            await api.get(url(import_id, rows=True), params=params, headers=me(imports_db))
        ).json()
        seen += [r["row_number"] for r in page["data"]]
        cursor = page["page"]["next_cursor"]
        assert (cursor is not None) is page["page"]["has_more"]
        if cursor is None:
            break
    assert seen == list(range(2, 52))


async def test_imports_rows_status_filter(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id = await done_import(imports_db, api)
    imported = (
        await api.get(
            url(import_id, rows=True),
            params={"status": "imported", "limit": "200"},
            headers=me(imports_db),
        )
    ).json()["data"]
    assert len(imported) == 45 and {r["status"] for r in imported} == {"imported"}
    assert all(r["product_id"] for r in imported)


async def test_imports_rows_no_raw_row(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id = await done_import(imports_db, api)
    response = await api.get(
        url(import_id, rows=True), params={"limit": "200"}, headers=me(imports_db)
    )
    assert "raw_row" not in response.text
    assert "Cotton Kurta Set 1" not in response.text  # a cell that only raw_row holds
    assert "cdn.example.com" not in response.text


async def test_imports_get_while_queued(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id, _ = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    response = await api.get(url(import_id), headers=me(imports_db))
    assert response.status_code == 200
    body = response.json()
    assert (body["total_rows"], body["imported"], body["failed"]) == (0, 0, 0)


async def test_imports_get_while_running(
    imports_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import_id, job_id = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    monkeypatch.setattr(runner, "BATCH_ROWS", 10)
    real = runner._run_batch
    gate = asyncio.Event()

    async def paused(run: Any, batch: Any) -> None:
        await real(run, batch)
        gate.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(runner, "_run_batch", paused)
    task = asyncio.create_task(run_job(job_id))
    try:
        await asyncio.wait_for(gate.wait(), 30)
        body = (await api.get(url(import_id), headers=me(imports_db))).json()
        assert (body["total_rows"], body["imported"], body["failed"]) == (10, 9, 1)
        envelope = (await api.get(f"{PREFIX}/jobs/{job_id}", headers=me(imports_db))).json()
        assert envelope["status"] == "running" and envelope["progress_pct"] == 20
        assert envelope["result_url"] == f"/api/v1/imports/{import_id}"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_imports_rows_during_retry(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    """A retry job is running (the import is `running` again, its failed rows untouched so far):
    every row still serialises."""
    import_id = await done_import(imports_db, api)
    imports_db.conn.execute(
        "UPDATE imports SET status = 'running', finished_at = NULL WHERE id = %s", [import_id]
    )
    report = (await api.get(url(import_id), headers=me(imports_db))).json()
    assert (report["total_rows"], report["imported"], report["failed"]) == (50, 45, 5)
    response = await api.get(
        url(import_id, rows=True), params={"limit": "200"}, headers=me(imports_db)
    )
    assert response.status_code == 200
    assert len(response.json()["data"]) == 50
