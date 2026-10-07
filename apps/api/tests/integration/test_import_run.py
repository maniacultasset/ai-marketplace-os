"""S13 B9 (DW2-3, AC 14-15; DATABASE.md:971-976): the `bulk_import` body for standalone rows,
through the real wrapper, as app_rw: row savepoints, totals from `import_rows`, progress, the
terminal hook mirroring the job onto the import, and no cell value anywhere it should not be."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from app.db.session import unit_of_work
from app.repositories.imports import ImportRowRepository
from app.services import job_hooks
from app.services.imports import row_model, runner
from mkos_schemas.enums import JobKind
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .imports_support import (
    csv_bytes,
    fixture_bytes,
    import_fixture,
    import_row,
    imports_db,
    one,
    row,
    row_results,
    run_job,
)
from .jobs_support import JobWorld, count, job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]
FIXTURE = "products_50_5_bad.csv"


def failed_codes(conn: Any, import_id: uuid.UUID) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for number, status, _sku, _pid, report in row_results(conn, import_id):
        if status == "failed":
            out[number] = [e.error_code.value for e in ErrorReport.model_validate(report).errors]
    return out


async def test_import_50_rows_5_bad(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id, job_id = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    await run_job(job_id)
    job = job_row(imports_db.conn, job_id)
    assert job["status"] == "partial", job["error_report_json"]
    imp = import_row(imports_db.conn, import_id)
    assert (imp["status"], imp["total_rows"], imp["imported_rows"], imp["failed_rows"]) == (
        "partial", 50, 45, 5,
    )  # fmt: skip
    assert imp["finished_at"] is not None
    tenant = imports_db.tenant("acme")
    assert (
        count(imports_db.conn, "SELECT count(*) FROM products WHERE tenant_id = %s", [tenant]) == 45
    )
    failed = [r for r in row_results(imports_db.conn, import_id) if r[1] == "failed"]
    assert [r[0] for r in failed] == [8, 15, 22, 35, 43]
    sources = sorted(ErrorReport.model_validate(r[4]).errors[0].source.value for r in failed)
    assert sources == ["rules_engine", "schema", "schema", "schema", "schema"]
    assert failed_codes(imports_db.conn, import_id) == {
        8: ["PATTERN_MISMATCH"],
        15: ["REQUIRED_FIELD_MISSING"],
        22: ["PRICE_ABOVE_MRP"],
        35: ["PATTERN_MISMATCH"],
        43: ["CATEGORY_NOT_MAPPED"],
    }
    assert count(imports_db.conn, "SELECT count(*) FROM import_rows WHERE status = 'pending'") == 0
    first = imports_db.conn.execute(
        "SELECT mrp, selling_price, tax_rate, hsn_code, country_of_origin, package_weight_g, "
        "source_image_urls, attributes -> 'color' ->> 'value', provenance -> 'mrp' ->> 'captured_via', "
        "status FROM products WHERE tenant_id = %s AND sku = 'FX-001'",
        [tenant],
    ).fetchone()
    assert first == (
        Decimal("1010.00"), Decimal("910.00"), Decimal("12.00"), "62114200", "IN", 301,
        ["https://cdn.example.com/fx/001-main.jpg", "https://cdn.example.com/fx/001-a.jpg",
         "https://cdn.example.com/fx/001-b.jpg"],
        "Maroon", "import", "draft",
    )  # fmt: skip


async def test_import_reupload_no_duplicate_sku(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = imports_db.tenant("acme")
    first_import, first = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    await run_job(first)
    stamps: dict[Any, Any] = dict(
        imports_db.conn.execute(
            "SELECT sku, updated_at FROM products WHERE tenant_id = %s", [tenant]
        ).fetchall()
    )
    imports_db.conn.execute(
        "UPDATE products SET deleted_at = now() WHERE tenant_id = %s AND sku = 'FX-002'", [tenant]
    )
    second_import, second = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    await run_job(second)
    assert job_row(imports_db.conn, second)["status"] == "partial"
    assert (
        count(imports_db.conn, "SELECT count(*) FROM products WHERE tenant_id = %s", [tenant]) == 45
    )
    revived = one(
        imports_db.conn,
        "SELECT deleted_at FROM products WHERE tenant_id = %s AND sku = 'FX-002'",
        [tenant],
    )
    assert revived is None
    after: dict[Any, Any] = dict(
        imports_db.conn.execute(
            "SELECT sku, updated_at FROM products WHERE tenant_id = %s AND sku <> 'FX-002'",
            [tenant],
        ).fetchall()
    )
    assert all(after[sku] == stamps[sku] for sku in after), "an unchanged row was rewritten"
    assert import_row(imports_db.conn, second_import)["imported_rows"] == 45
    assert first_import != second_import


async def test_import_duplicate_sku_in_file(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    data = csv_bytes([row(1), row(2), row(1, product_name="Again"), row(3)])
    import_id, job_id = await import_fixture(imports_db, api, data)
    await run_job(job_id)
    assert failed_codes(imports_db.conn, import_id) == {4: ["DUPLICATE_SKU_IN_FILE"]}
    title = one(
        imports_db.conn,
        "SELECT title FROM products WHERE tenant_id = %s AND sku = 'SKU-00001'",
        [imports_db.tenant("acme")],
    )
    assert title == "Cotton Kurta 1"


async def test_import_totals_from_rows(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id, job_id = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    await run_job(job_id)
    by_status: dict[Any, Any] = dict(
        imports_db.conn.execute(
            "SELECT status, count(*) FROM import_rows WHERE import_id = %s GROUP BY status",
            [import_id],
        ).fetchall()
    )
    imp = import_row(imports_db.conn, import_id)
    assert (imp["imported_rows"], imp["failed_rows"]) == (
        by_status["imported"],
        by_status["failed"],
    )
    result = job_row(imports_db.conn, job_id)["result_json"]
    assert (result["imported"], result["failed"], result["total_rows"]) == (45, 5, 50)


async def test_import_progress_in_result_json(
    imports_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    from app.repositories.jobs import JobRunRepository

    real = JobRunRepository.set_result

    async def spy(self: Any, job_id: uuid.UUID, attempt: int, result: dict[str, Any]) -> bool:
        seen.append(dict(result))
        return await real(self, job_id, attempt, result)

    monkeypatch.setattr(runner, "BATCH_ROWS", 10)
    monkeypatch.setattr(JobRunRepository, "set_result", spy)
    import_id, job_id = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    await run_job(job_id)
    assert [s["progress_pct"] for s in seen] == [20, 40, 60, 80, 99]
    assert {s["import_id"] for s in seen} == {str(import_id)}
    final = job_row(imports_db.conn, job_id)["result_json"]
    assert (final["import_id"], final["progress_pct"]) == (str(import_id), 100)


async def test_import_no_generation_enqueued(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    _, job_id = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    await run_job(job_id)
    kinds = imports_db.conn.execute("SELECT DISTINCT kind FROM job_runs").fetchall()
    assert kinds == [("bulk_import",)]
    assert count(imports_db.conn, "SELECT count(*) FROM batches") == 0
    assert count(imports_db.conn, "SELECT count(*) FROM listings") == 0


@pytest.mark.parametrize("ending", ["partial", "succeeded", "failed", "cancelled"])
async def test_import_imports_status_mirrors_job(
    imports_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    data = fixture_bytes(FIXTURE) if ending == "partial" else csv_bytes([row(1), row(2)])
    import_id, job_id = await import_fixture(imports_db, api, data)
    if ending == "cancelled":
        from .imports_support import headers

        response = await api.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers(imports_db))
        assert response.status_code == 200
    else:
        if ending == "failed":
            imports_db.conn.execute(
                "UPDATE imports SET storage_key = 'elsewhere/x.csv' WHERE id = %s", [import_id]
            )
        await run_job(job_id)
    job = job_row(imports_db.conn, job_id)
    imp = import_row(imports_db.conn, import_id)
    assert (job["status"], imp["status"]) == (ending, ending)
    if ending == "failed":
        assert imp["error_report_json"]["error_code"] == "IMPORT_FAILED"


async def test_import_rows_scoped_to_tenant(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id, job_id = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    await run_job(job_id)
    tenants = imports_db.conn.execute(
        "SELECT DISTINCT tenant_id FROM import_rows WHERE import_id = %s", [import_id]
    ).fetchall()
    assert tenants == [(imports_db.tenant("acme"),)]
    bharat = imports_db.tenant("bharat")
    async with unit_of_work(bharat) as session:
        rows, more = await ImportRowRepository(session, bharat).page(
            import_id, status=None, after=None, limit=200
        )
        assert (rows, more) == ([], False)
        totals = await ImportRowRepository(session, bharat).totals(import_id)
        assert totals.total == 0


async def test_import_row_db_error_is_row_failure(
    imports_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that passes the row model but trips a database CHECK (a patched SKU rule), and a
    unique violation (two SKUs with one GTIN): those rows fail, the rest import."""
    monkeypatch.setattr(row_model, "SKU_PATTERN", re.compile(r".{1,64}"))
    gtin = "4006381333931"
    data = csv_bytes(
        [
            row(1),
            row(2, seller_sku="BAD]SKU"),
            row(3, product_identifier_type="ean", product_identifier=gtin),
            row(4, product_identifier_type="ean", product_identifier=gtin),
            row(5),
        ]
    )
    import_id, job_id = await import_fixture(imports_db, api, data)
    await run_job(job_id)
    assert job_row(imports_db.conn, job_id)["status"] == "partial"
    assert failed_codes(imports_db.conn, import_id) == {
        3: ["PATTERN_MISMATCH"],
        5: ["IDENTIFIER_INVALID"],
    }
    assert import_row(imports_db.conn, import_id)["imported_rows"] == 3


async def test_import_db_constraint_row_isolated(
    imports_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mrp=0` and `mrp=1e11` with the row model's bounds bypassed: the CHECK and the numeric
    overflow fail those rows only, and no message carries the cell."""
    monkeypatch.setattr(row_model, "MONEY_PATTERN", re.compile(r"[0-9]{1,13}(\.[0-9]{1,2})?"))
    monkeypatch.setattr(row_model._Checker, "positive", lambda self, key, number: number)
    data = csv_bytes(
        [
            row(1),
            row(2, mrp="0", selling_price=""),
            row(3, mrp="100000000000", selling_price=""),
            row(4),
        ]
    )
    import_id, job_id = await import_fixture(imports_db, api, data)
    await run_job(job_id)
    assert job_row(imports_db.conn, job_id)["status"] == "partial"
    assert failed_codes(imports_db.conn, import_id) == {
        3: ["VALUE_NOT_ALLOWED"],
        4: ["VALUE_NOT_ALLOWED"],
    }
    for _number, status, _sku, _pid, report in row_results(imports_db.conn, import_id):
        if status == "failed":
            text = str(report)
            assert "100000000000" not in text and "'0'" not in text
            assert ErrorReport.model_validate(report).errors[0].value_submitted is None


async def test_import_old_job_hook_fenced(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    """An import handed to a newer job: the old job's run refuses it and its terminal hook
    writes nothing onto the import."""
    import_id, old_job = await import_fixture(imports_db, api, csv_bytes([row(1)]))
    newer = imports_db.conn.execute(
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
        "correlation_id, parent_job_id) VALUES (%s, 'bulk_import', 'import', %s, '{}', %s, NULL) "
        "RETURNING id",
        [imports_db.tenant("acme"), import_id, uuid.uuid4()],
    ).fetchone()
    assert newer is not None
    imports_db.conn.execute(
        "UPDATE imports SET job_id = %s, status = 'queued' WHERE id = %s", [newer[0], import_id]
    )
    await run_job(old_job)
    assert job_row(imports_db.conn, old_job)["status"] == "failed"
    imp = import_row(imports_db.conn, import_id)
    assert (imp["job_id"], imp["status"], imp["finished_at"]) == (newer[0], "queued", None)
    assert (
        count(imports_db.conn, "SELECT count(*) FROM import_rows WHERE import_id = %s", [import_id])
        == 0
    )


async def test_import_run_no_cell_value_logged(
    imports_db: JobWorld,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    log_lines: Any,
    sentry_events: Any,
) -> None:
    from app.core.config import Settings
    from app.core.observability import init_sentry

    canary = "CANARY-cell-5b17"
    init_sentry(
        Settings(
            _env_file=None,
            app_env="local",
            app_role="shell",
            sentry_dsn="https://k@o1.ingest.sentry.io/1",
        )
    )
    capture = log_lines()
    real = runner._Writer._row_facts

    async def explode(self: Any, product_id: Any, row: Any, *, level: str) -> None:
        if row.title.endswith("boom"):
            raise RuntimeError(row.title)  # an unexpected body error quoting the cell
        await real(self, product_id, row, level=level)

    data = csv_bytes(
        [
            row(1, product_name=f"{canary} good"),
            row(2, mrp=canary),
            row(3, category=canary.lower().replace("-", "_")),
            row(4, product_name=f"{canary} boom"),
        ]
    )
    monkeypatch.setattr(runner, "BATCH_ROWS", 3)
    import_id, job_id = await import_fixture(imports_db, api, data)
    monkeypatch.setattr(runner._Writer, "_row_facts", explode)
    await run_job(job_id)
    job = job_row(imports_db.conn, job_id)
    assert job["status"] == "failed"  # the unexpected error fails the job, not the batch silently
    assert job["error_report_json"]["errors"][0]["message"] == "the job failed (RuntimeError)"
    rows = str(row_results(imports_db.conn, import_id))
    assert "PATTERN_MISMATCH" in rows and "CATEGORY_NOT_MAPPED" in rows  # batch 1 committed
    for text in (capture.text(), sentry_events.serialized(), str(job["error_report_json"]), rows):
        assert canary not in text and canary.lower().replace("-", "_") not in text
    assert job_hooks.TERMINAL_HOOKS[JobKind.BULK_IMPORT] is not None
