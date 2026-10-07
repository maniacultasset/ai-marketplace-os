"""S13 A9 (DW8, AC5; DATABASE.md:1028; spike REPORT C8): the lease reaper. A `kill -9`ed worker's
job is recovered by the reaper and run again by the next worker (the attempt counts); an exhausted
one is `dead_letter` + `JOB_TIMEOUT`; the dead queue entry never runs; two reapers requeue once."""

from __future__ import annotations

import asyncio
import signal
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.db.queue import new_queue_app
from app.db.session import unit_of_work
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.services import job_hooks
from app.workers.periodic import reap_once
from app.workers.registry import JobContext, Outcome
from mkos_schemas.enums import JobKind
from mkos_schemas.errors import ErrorReport

from .auth_support import add_tenant
from .conftest import LOCAL_HOSTS
from .jobs_support import (
    JobWorld,
    count,
    eventually,
    job_row,
    jobs_db,
    override_body,
    queue_row,
    running_worker,
)
from .test_worker_sigterm import spawn_worker

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]
KIND = JobKind.BULK_IMPORT


async def _enqueue(jw: JobWorld, tenant: uuid.UUID, *, sleep: float = 0) -> tuple[uuid.UUID, str]:
    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(
                kind=KIND,
                entity_type="import",
                entity_id=uuid.uuid4(),
                created_by=None,
                payload={"sleep": sleep},
            )
        )
    assert done.job.queue_job_id is not None
    return done.job.id, done.job.queue_job_id


def _as_dead_worker(jw: JobWorld, job: uuid.UUID, qid: str, *, attempt: int = 1) -> None:
    """What a worker killed mid-job leaves: the row `running` with an expired lease, the queue
    entry `doing`."""
    jw.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = %s, started_at = now(), "
        "lease_expires_at = now() - interval '1 second' WHERE id = %s",
        [attempt, job],
    )
    jw.conn.execute("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", [int(qid)])


@pytest.fixture
def hook_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []

    async def hook(session: Any, job: Any, outcome: Any) -> None:
        calls.append((job.status, outcome))

    monkeypatch.setitem(job_hooks.TERMINAL_HOOKS, KIND, hook)
    return calls


async def test_worker_crash_recovery(jobs_db: JobWorld, tmp_path: Path) -> None:
    job, old_qid = await _enqueue(jobs_db, jobs_db.tenant("acme"), sleep=120)
    # a 2 s timeout and a 1 s grace: the claimed lease expires 3 s after the claim
    proc = spawn_worker(tmp_path, "--timeout", "2", "--lease-grace", "1")
    try:
        await eventually(lambda: job_row(jobs_db.conn, job)["status"] == "running", 30)
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.communicate(timeout=30)
    assert job_row(jobs_db.conn, job)["attempt"] == 1
    lease = job_row(jobs_db.conn, job)["lease_expires_at"]
    await asyncio.sleep(max(0.0, (lease - datetime.now(UTC)).total_seconds()) + 0.5)
    app = new_queue_app()
    async with app.open_async():
        result = await reap_once(app.job_manager)
    assert result.requeued == [job]
    assert queue_row(jobs_db.conn, old_qid)["status"] == "failed"
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"], row["lease_expires_at"]) == ("queued", 1, None)
    assert row["queue_job_id"] != old_qid
    nxt = spawn_worker(tmp_path, "--max-sleep", "0.1")
    try:
        await eventually(lambda: job_row(jobs_db.conn, job)["status"] == "succeeded", 30)
    finally:
        nxt.send_signal(signal.SIGTERM)
        nxt.communicate(timeout=30)
    row = job_row(jobs_db.conn, job)
    assert (row["attempt"], row["result_json"]["pid"]) == (2, nxt.pid)


async def test_reaper_exhausted_dead_letter_job_timeout(
    jobs_db: JobWorld, hook_calls: list[Any]
) -> None:
    job, qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    _as_dead_worker(jobs_db, job, qid, attempt=3)
    result = await reap_once()
    assert result.dead_lettered == [job]
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["error_code"]) == ("dead_letter", "JOB_TIMEOUT")
    assert row["finished_at"] is not None and row["lease_expires_at"] is None
    report = ErrorReport.model_validate(row["error_report_json"])
    assert report.attempts_used == 3 and report.errors[0].source.value == "system"
    assert queue_row(jobs_db.conn, qid)["status"] == "failed"
    assert count(jobs_db.conn, "SELECT count(*) FROM procrastinate_jobs") == 1
    assert hook_calls == [("dead_letter", None)]


async def test_reaper_old_queue_entry_never_runs(jobs_db: JobWorld, override_body: Any) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(ctx.attempt)
        return Outcome("succeeded")

    override_body(KIND, body)
    job, old_qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    _as_dead_worker(jobs_db, job, old_qid)
    await reap_once()
    # even if the dead entry were handed back to `todo`, the CAS would drop it: here it is failed
    assert queue_row(jobs_db.conn, old_qid)["status"] == "failed"
    async with running_worker():
        await eventually(lambda: job_row(jobs_db.conn, job)["status"] == "succeeded")
        await asyncio.sleep(0.5)
    assert runs == [2]
    assert queue_row(jobs_db.conn, old_qid)["status"] == "failed"


async def test_reaper_skips_unexpired(jobs_db: JobWorld) -> None:
    job, qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    _as_dead_worker(jobs_db, job, qid)
    jobs_db.conn.execute(
        "UPDATE job_runs SET lease_expires_at = now() + interval '1 hour' WHERE id = %s", [job]
    )
    before = job_row(jobs_db.conn, job)
    result = await reap_once()
    assert result.requeued == result.dead_lettered == result.cancelled == []
    assert job_row(jobs_db.conn, job) == before
    assert queue_row(jobs_db.conn, qid)["status"] == "doing"


async def test_reaper_prunes_stale_workers(jobs_db: JobWorld) -> None:
    jobs_db.conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (%s), (now())",
        [datetime.now(UTC) - timedelta(minutes=10)],
    )
    app = new_queue_app()
    async with app.open_async():
        result = await reap_once(app.job_manager)
    assert len(result.pruned_workers) == 1
    assert count(jobs_db.conn, "SELECT count(*) FROM procrastinate_workers") == 1


async def test_reaper_two_tenants_written_per_tenant(jobs_db: JobWorld) -> None:
    a, qa = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    b, qb = await _enqueue(jobs_db, jobs_db.tenant("bharat"))
    _as_dead_worker(jobs_db, a, qa)
    _as_dead_worker(jobs_db, b, qb)
    result = await reap_once()
    assert sorted(result.requeued) == sorted([a, b])
    for job, tenant in ((a, "acme"), (b, "bharat")):
        row = job_row(jobs_db.conn, job)
        assert (row["status"], row["tenant_id"]) == ("queued", jobs_db.tenant(tenant))
        assert queue_row(jobs_db.conn, row["queue_job_id"])["args"] == {"job_run_id": str(job)}


async def test_reaper_two_concurrent_reapers_one_requeue(jobs_db: JobWorld) -> None:
    job, qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    _as_dead_worker(jobs_db, job, qid)
    first, second = await asyncio.gather(reap_once(), reap_once())
    assert len(first.requeued) + len(second.requeued) == 1
    assert count(jobs_db.conn, "SELECT count(*) FROM procrastinate_jobs WHERE status = 'todo'") == 1
    assert job_row(jobs_db.conn, job)["status"] == "queued"


async def test_reaper_skips_suspended_tenant(jobs_db: JobWorld, hook_calls: list[Any]) -> None:
    tenant = add_tenant(jobs_db.conn, "acme-suspended")
    job, qid = await _enqueue(jobs_db, tenant)
    _as_dead_worker(jobs_db, job, qid)
    jobs_db.conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", [tenant])
    result = await reap_once()
    assert result.cancelled == [job]
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["lease_expires_at"]) == ("cancelled", None)
    assert queue_row(jobs_db.conn, qid)["status"] == "failed"
    assert count(jobs_db.conn, "SELECT count(*) FROM procrastinate_jobs WHERE status = 'todo'") == 0
    assert hook_calls == [("cancelled", None)]
