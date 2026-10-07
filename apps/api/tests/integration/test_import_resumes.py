"""S13 B12 (AC 18; spike REPORT §5; Q12): an import resumes from its unfinished units after a
`kill -9` (the A9 reaper re-queues it) or a shutdown abort (released uncounted); a re-run of
the body is idempotent; a scattered family is re-run whole when any of its rows is unfinished."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.db.session import unit_of_work
from app.repositories.imports import ImportRowRepository, RowWrite
from app.services.imports import runner
from app.workers.periodic import reap_once
from app.workers.registry import JobContext
from mkos_schemas.enums import JobKind

from .conftest import LOCAL_HOSTS
from .imports_support import (
    csv_bytes,
    fixture_bytes,
    import_fixture,
    import_row,
    imports_db,
    row,
    run_job,
)
from .jobs_support import JobWorld, count, eventually, insert_job, job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]
FIXTURE = "products_50_5_bad.csv"
RUN_IMPORT_WORKER = Path(__file__).resolve().parents[1] / "support" / "run_import_worker.py"


def rows_done(jw: JobWorld, import_id: uuid.UUID) -> int:
    return count(jw.conn, "SELECT count(*) FROM import_rows WHERE import_id = %s", [import_id])


def spawn(tmp_path: Path, *args: str) -> subprocess.Popen[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "APP_ENV": "local",
        "APP_ROLE": "worker",
        "DATABASE_URL": os.environ["DATABASE_URL"],  # app_rw (the auth_db fixture)
        "S3_ENDPOINT_URL": os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000"),
        "LOG_FORMAT": "json",
    }
    return subprocess.Popen(
        [sys.executable, str(RUN_IMPORT_WORKER), *args],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def assert_finished(jw: JobWorld, import_id: uuid.UUID, job_id: uuid.UUID) -> None:
    assert job_row(jw.conn, job_id)["status"] == "partial"
    imp = import_row(jw.conn, import_id)
    assert (imp["total_rows"], imp["imported_rows"], imp["failed_rows"]) == (50, 45, 5)
    assert rows_done(jw, import_id) == 50
    tenant = jw.tenant("acme")
    assert count(jw.conn, "SELECT count(*) FROM products WHERE tenant_id = %s", [tenant]) == 45


async def test_import_resumes(imports_db: JobWorld, api: httpx.AsyncClient, tmp_path: Path) -> None:
    import_id, job_id = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    proc = spawn(tmp_path, "--batch-rows", "10", "--pause", "5")
    try:
        await eventually(lambda: rows_done(imports_db, import_id) >= 10, 60)
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.communicate(timeout=30)
    done_before = rows_done(imports_db, import_id)
    assert 10 <= done_before < 50
    row_before = job_row(imports_db.conn, job_id)
    assert (row_before["status"], row_before["attempt"]) == ("running", 1)
    imports_db.conn.execute(
        "UPDATE job_runs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        [job_id],
    )
    assert (await reap_once()).requeued == [job_id]
    assert job_row(imports_db.conn, job_id)["status"] == "queued"
    resumed: list[int] = []
    real = runner._run_batch

    async def spy(run: Any, batch: list[runner.Unit]) -> None:
        resumed.extend(n for unit in batch for n in unit.row_numbers)
        await real(run, batch)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner, "_run_batch", spy)
        await run_job(job_id)
    assert len(resumed) == 50 - done_before  # only the unfinished units ran again
    assert job_row(imports_db.conn, job_id)["attempt"] == 2
    assert_finished(imports_db, import_id, job_id)


async def test_import_shutdown_abort_resumes(
    imports_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shutdown abort (the worker's SIGTERM path cancels the body) releases the job uncounted
    after a committed batch; the next run resumes."""
    import_id, job_id = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    monkeypatch.setattr(runner, "BATCH_ROWS", 10)
    real = runner._run_batch
    gate = asyncio.Event()

    async def paused(run: Any, batch: list[runner.Unit]) -> None:
        await real(run, batch)
        gate.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(runner, "_run_batch", paused)
    task = asyncio.create_task(run_job(job_id))
    await asyncio.wait_for(gate.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = job_row(imports_db.conn, job_id)
    assert (row["status"], row["attempt"]) == ("queued", 0)
    assert rows_done(imports_db, import_id) == 10
    monkeypatch.setattr(runner, "_run_batch", real)
    await run_job(job_id)
    assert_finished(imports_db, import_id, job_id)


async def test_import_body_rerun_idempotent(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id, job_id = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
    await run_job(job_id)
    tenant = imports_db.tenant("acme")
    before = imports_db.conn.execute(
        "SELECT sku, updated_at FROM products WHERE tenant_id = %s ORDER BY sku", [tenant]
    ).fetchall()
    rows_before = imports_db.conn.execute(
        "SELECT row_number, status, product_id FROM import_rows WHERE import_id = %s "
        "ORDER BY row_number",
        [import_id],
    ).fetchall()
    imports_db.conn.execute("DELETE FROM import_rows WHERE import_id = %s", [import_id])
    # a second attempt of the same body: a running job that owns the import
    rerun = insert_job(
        imports_db.conn,
        tenant,
        entity_id=import_id,
        status="running",
        attempt=1,
        started_at=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    imports_db.conn.execute("UPDATE imports SET job_id = %s WHERE id = %s", [rerun, import_id])
    ctx = JobContext(rerun, tenant, JobKind.BULK_IMPORT, "req-rerun", {}, 1)
    outcome = await runner.run_import(ctx)
    assert outcome.status == "partial"
    after = imports_db.conn.execute(
        "SELECT sku, updated_at FROM products WHERE tenant_id = %s ORDER BY sku", [tenant]
    ).fetchall()
    assert after == before
    rows_after = imports_db.conn.execute(
        "SELECT row_number, status, product_id FROM import_rows WHERE import_id = %s "
        "ORDER BY row_number",
        [import_id],
    ).fetchall()
    assert rows_after == rows_before
    assert job_row(imports_db.conn, rerun)["result_json"]["progress_pct"] == 99


async def test_import_resumes_scattered_family(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """A commit held the unit of rows 900-950 (a family) but not row 2's unit: the resumed run
    runs row 2's unit and skips the finished family; a family with one unfinished row runs whole."""
    family = [
        row(
            n,
            parent_style_id="FAM",
            size_name=f"s{n}",
            product_name="Tee",
            category="ethnic_wear_kurta_sets",
        )
        for n in range(1, 52)
    ]
    singles = [row(n) for n in range(2000, 2898)]
    data = csv_bytes([singles[0], *singles[1:898], *family])
    import_id, job_id = await import_fixture(imports_db, api, data)
    tenant = imports_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        repo = ImportRowRepository(session, tenant)
        for number in range(900, 951):  # the family's rows, as a committed batch left them
            await repo.write(import_id, job_id, RowWrite(number, "imported", {}, sku=f"x{number}"))
    ran: list[set[int]] = []
    real = runner._run_batch

    async def spy(run: Any, batch: list[runner.Unit]) -> None:
        ran.extend(unit.row_numbers for unit in batch)
        await real(run, batch)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner, "_run_batch", spy)
        await run_job(job_id)
    ran_rows = set().union(*ran)
    assert 2 in ran_rows
    assert not ran_rows & set(range(900, 951))
    assert ran_rows == set(range(2, 900))
    imp = import_row(imports_db.conn, import_id)
    assert imp["total_rows"] == 949
