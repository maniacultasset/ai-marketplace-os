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
from app.workers.policy import image_job_lock
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
    assert _lock(jobs_db, qid) == image_job_lock(jobs_db.tenant("acme"))
    jobs_db.conn.execute("UPDATE job_runs SET status = 'queued', attempt = 1 WHERE id = %s", [job])
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = NULL WHERE id = %s",
        [int(qid)],
    )
    result = await reap_once()
    assert result.redeferred == [job]
    new_qid = job_row(jobs_db.conn, job)["queue_job_id"]
    assert _lock(jobs_db, new_qid) == image_job_lock(jobs_db.tenant("acme"))


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
    assert _lock(jobs_db, row["queue_job_id"]) == image_job_lock(jobs_db.tenant("acme"))


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
    assert _lock(jobs_db, new.queue_job_id) == image_job_lock(jobs_db.tenant("acme"))


# ---- SI7 T11 (design §2.2): a candidate job keeps its tenant's lock on every re-defer ----


async def _enqueue_candidate_job(tenant: uuid.UUID) -> tuple[uuid.UUID, str]:
    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type="image_candidate",
                entity_id=uuid.uuid4(),
                created_by=None,
                payload={"candidate_id": str(uuid.uuid4())},
            )
        )
    assert done.job.queue_job_id is not None
    return done.job.id, done.job.queue_job_id


async def test_candidate_orphan_redefer_keeps_tenant_lock(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    job, qid = await _enqueue_candidate_job(tenant)
    assert _lock(jobs_db, qid) == f"image_candidate:{tenant}"
    jobs_db.conn.execute("UPDATE job_runs SET status = 'queued', attempt = 1 WHERE id = %s", [job])
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = NULL WHERE id = %s",
        [int(qid)],
    )
    result = await reap_once()
    assert result.redeferred == [job]
    new_qid = job_row(jobs_db.conn, job)["queue_job_id"]
    assert _lock(jobs_db, new_qid) == f"image_candidate:{tenant}"


async def test_candidate_lease_reaper_requeue_keeps_tenant_lock(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    job, qid = await _enqueue_candidate_job(tenant)
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
    assert _lock(jobs_db, row["queue_job_id"]) == f"image_candidate:{tenant}"


async def test_publish_entries_outrank_status_syncs(jobs_db: JobWorld) -> None:
    """V1 hardening #8: within the shared `publish` queue a publish entry carries a higher
    procrastinate priority than a status sync, so a burst of syncs never delays it."""
    from app.db.queue import defer_on_session
    from app.workers.policy import PUBLISH_PRIORITY, priority_for

    assert priority_for(JobKind.LISTING_PUBLISH) == PUBLISH_PRIORITY > 0
    assert priority_for(JobKind.LISTING_STATUS_SYNC) == 0
    tenant = jobs_db.tenant("acme")
    qids: dict[str, str] = {}
    async with unit_of_work(tenant) as session:
        for kind in (JobKind.LISTING_STATUS_SYNC, JobKind.LISTING_PUBLISH):
            qids[kind.value] = await defer_on_session(
                session,
                kind=kind.value,
                queue="publish",
                job_run_id=uuid.uuid4(),
                priority=priority_for(kind),
            )
    rows: dict[str, int] = dict(
        jobs_db.conn.execute(
            "SELECT task_name, priority FROM procrastinate_jobs WHERE id = ANY(%s)",
            [[int(q) for q in qids.values()]],
        ).fetchall()
    )
    assert rows == {"listing_status_sync": 0, "listing_publish": PUBLISH_PRIORITY}
