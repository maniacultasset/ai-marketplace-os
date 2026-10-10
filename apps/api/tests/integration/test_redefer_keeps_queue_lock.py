"""S26 fix round 1: every re-defer of an image_compliance job keeps its procrastinate `lock`.

S17b serialises image_compliance jobs with lock `image_compliance`. The orphan re-defer, the lease
reaper's requeue and the staff requeue each make a new queue entry; each now derives the lock from
the kind (`app.workers.policy.lock_for`), so the new entry never runs beside another image job
(KNOWN-ISSUES §E "Image-pipeline follow-ups").
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.db.session import unit_of_work
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.workers.periodic import reap_once
from app.workers.policy import IMAGE_JOB_LOCK
from mkos_schemas.enums import JobKind

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, job_row, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]


async def _enqueue_image_job(tenant: uuid.UUID) -> tuple[uuid.UUID, str]:
    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type="image",
                entity_id=uuid.uuid4(),
                created_by=None,
                payload={"image_id": str(uuid.uuid4())},
            )
        )
    assert done.job.queue_job_id is not None
    return done.job.id, done.job.queue_job_id


def _lock(jw: JobWorld, qid: str) -> Any:
    row = jw.conn.execute(
        "SELECT lock FROM procrastinate_jobs WHERE id = %s", [int(qid)]
    ).fetchone()
    assert row is not None
    return row[0]


async def test_orphan_redefer_keeps_image_lock(jobs_db: JobWorld) -> None:
    job, qid = await _enqueue_image_job(jobs_db.tenant("acme"))
    assert _lock(jobs_db, qid) == IMAGE_JOB_LOCK
    jobs_db.conn.execute("UPDATE job_runs SET status = 'queued', attempt = 1 WHERE id = %s", [job])
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = NULL WHERE id = %s",
        [int(qid)],
    )
    result = await reap_once()
    assert result.redeferred == [job]
    new_qid = job_row(jobs_db.conn, job)["queue_job_id"]
    assert _lock(jobs_db, new_qid) == IMAGE_JOB_LOCK


async def test_lease_reaper_requeue_keeps_image_lock(jobs_db: JobWorld) -> None:
    job, qid = await _enqueue_image_job(jobs_db.tenant("acme"))
    jobs_db.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = 1, max_attempts = 3, "
        "started_at = now(), lease_expires_at = now() - interval '1 second' WHERE id = %s",
        [job],
    )
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = NULL WHERE id = %s",
        [int(qid)],
    )
    await reap_once()
    row = job_row(jobs_db.conn, job)
    assert row["status"] == "queued"
    assert row["queue_job_id"] != qid
    assert _lock(jobs_db, row["queue_job_id"]) == IMAGE_JOB_LOCK


async def test_staff_requeue_keeps_image_lock(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    job, _ = await _enqueue_image_job(tenant)
    jobs_db.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = 3, started_at = now(), "
        "lease_expires_at = now() + interval '1 hour' WHERE id = %s",
        [job],
    )
    jobs_db.conn.execute(
        "UPDATE job_runs SET status = 'dead_letter', lease_expires_at = NULL, "
        "finished_at = now(), error_report_json = '{\"errors\": []}' WHERE id = %s",
        [job],
    )
    async with unit_of_work(tenant) as session:
        _, new = await JobRunRepository(session, tenant).requeue(job)
    assert new.queue_job_id is not None
    assert _lock(jobs_db, new.queue_job_id) == IMAGE_JOB_LOCK
