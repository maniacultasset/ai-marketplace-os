"""S26 (KNOWN-ISSUES §E, S13a): a worker killed after `release_retry`/`release_shutdown`
committed, before the queue re-queued its entry, leaves the row `queued` and the entry `doing`
with its worker pruned. The reaper gives such a row a new entry (the old one retired), so it runs
once more; a row whose entry belongs to a live worker, or is plainly `todo`, is left alone."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.db.queue import new_queue_app
from app.workers.periodic import reap_once
from app.workers.registry import JobContext, Outcome

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
from .test_worker_crash_recovery import KIND, _enqueue

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]


def _as_orphan(jw: JobWorld, job: uuid.UUID, qid: str) -> None:
    """The row released (`queued`, attempt kept), its entry `doing` with no worker."""
    jw.conn.execute(
        "UPDATE job_runs SET status = 'queued', attempt = 1, started_at = NULL, "
        "lease_expires_at = NULL WHERE id = %s",
        [job],
    )
    jw.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = NULL WHERE id = %s",
        [int(qid)],
    )


async def test_reaper_redefers_orphaned_queued_row(jobs_db: JobWorld, override_body: Any) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(ctx.attempt)
        return Outcome("succeeded")

    override_body(KIND, body)
    job, old_qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    _as_orphan(jobs_db, job, old_qid)
    result = await reap_once()
    assert result.redeferred == [job]
    row = job_row(jobs_db.conn, job)
    assert row["status"] == "queued" and row["queue_job_id"] != old_qid
    assert queue_row(jobs_db.conn, old_qid)["status"] == "failed"
    assert queue_row(jobs_db.conn, row["queue_job_id"])["status"] == "todo"
    again = await reap_once()  # idempotent: the new entry is `todo`, nothing more to do
    assert again.redeferred == []
    async with running_worker():
        await eventually(lambda: job_row(jobs_db.conn, job)["status"] == "succeeded")
        await asyncio.sleep(0.5)
    assert runs == [2]  # the released attempt counted; it ran once more, once


async def test_reaper_leaves_live_worker_and_todo_entries(jobs_db: JobWorld) -> None:
    live, live_qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    todo, todo_qid = await _enqueue(jobs_db, jobs_db.tenant("bharat"))
    worker = jobs_db.conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now()) RETURNING id"
    ).fetchone()
    assert worker is not None
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
        [worker[0], int(live_qid)],
    )
    before = (job_row(jobs_db.conn, live), job_row(jobs_db.conn, todo))
    result = await reap_once()
    assert result.redeferred == []
    assert (job_row(jobs_db.conn, live), job_row(jobs_db.conn, todo)) == before
    assert queue_row(jobs_db.conn, live_qid)["status"] == "doing"
    assert queue_row(jobs_db.conn, todo_qid)["status"] == "todo"


async def test_reaper_redefers_after_pruning_the_dead_worker(jobs_db: JobWorld) -> None:
    """End to end: the stale worker is pruned in the same pass, then its orphan re-deferred."""
    job, qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    stale = jobs_db.conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (%s) RETURNING id",
        [datetime.now(UTC) - timedelta(minutes=10)],
    ).fetchone()
    assert stale is not None
    jobs_db.conn.execute("UPDATE job_runs SET status = 'queued', attempt = 1 WHERE id = %s", [job])
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
        [stale[0], int(qid)],
    )
    app = new_queue_app()
    async with app.open_async():
        result = await reap_once(app.job_manager)
    assert stale[0] in result.pruned_workers
    assert result.redeferred == [job]
    assert count(jobs_db.conn, "SELECT count(*) FROM procrastinate_jobs WHERE status = 'todo'") == 1
