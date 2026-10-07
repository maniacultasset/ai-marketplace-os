"""S13a fix round 1, F1 (tester repro `test_s13a_r1_reaper_gaps.py`): one job whose reap raises
(here its terminal hook) rolls back alone; the reaper logs it, skips it and recovers every other
tenant's crashed job in the same pass. As app_rw, through the real reaper."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.db.session import unit_of_work
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.services import job_hooks
from app.workers.periodic import reap_once
from mkos_schemas.enums import JobKind

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, job_row, jobs_db, override_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]


async def _enqueue(tenant: uuid.UUID, kind: JobKind) -> tuple[uuid.UUID, str]:
    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(kind, "import", uuid.uuid4(), None)
        )
    assert done.job.queue_job_id is not None
    return done.job.id, done.job.queue_job_id


def _dead_worker(jw: JobWorld, job: uuid.UUID, qid: str, attempt: int, age_s: int) -> None:
    jw.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = %s, started_at = now(), "
        "lease_expires_at = now() - make_interval(secs => %s) WHERE id = %s",
        [attempt, age_s, job],
    )
    jw.conn.execute("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", [int(qid)])


async def test_reaper_one_failing_job_does_not_block_the_rest(
    jobs_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_hook(session: Any, job: Any, outcome: Any) -> None:
        raise RuntimeError("hook bug")

    monkeypatch.setitem(job_hooks.TERMINAL_HOOKS, JobKind.IMAGE_COMPLIANCE, broken_hook)
    # acme: an exhausted image job with the oldest lease (its dead-letter hook raises)
    poison, pq = await _enqueue(jobs_db.tenant("acme"), JobKind.IMAGE_COMPLIANCE)
    _dead_worker(jobs_db, poison, pq, attempt=3, age_s=600)
    # bharat: an ordinary crashed import with attempts left
    victim, vq = await _enqueue(jobs_db.tenant("bharat"), JobKind.BULK_IMPORT)
    _dead_worker(jobs_db, victim, vq, attempt=1, age_s=5)

    result = await reap_once()

    assert result.skipped == [poison]
    assert result.requeued == [victim]
    assert job_row(jobs_db.conn, victim)["status"] == "queued"
    # the failing job's transaction rolled back alone: still running, retried next pass
    poisoned = job_row(jobs_db.conn, poison)
    assert (poisoned["status"], poisoned["attempt"]) == ("running", 3)
    again = await reap_once()
    assert again.skipped == [poison]
    assert job_row(jobs_db.conn, poison)["status"] == "running"
