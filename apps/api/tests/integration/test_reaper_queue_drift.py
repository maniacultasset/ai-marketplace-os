"""Audit JOBS-2 and JOBS-4: the reaper reconciles the queue with `job_runs`.

- JOBS-4: a lost procrastinate `finish_job` (a DB blip on the queue pool right after the
  wrapper's terminal transaction, or a kill in that window) left the `image_compliance` entry
  `doing` while its row was terminal. procrastinate never fetches a job whose lock has a `doing`
  entry, so every later image job of every tenant stalled. The reaper now retires such an entry.
  A lost `retry_job` left the row `queued` and the entry `doing` under a live worker; past the
  kind's lease the reaper re-defers it.
- JOBS-2: after `INFRA_RETRY_CAP` infrastructure errors the queue finishes the entry `failed`
  while the row is still `queued`. The reaper now ends the row `dead_letter` + `INTERNAL_ERROR`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from app.db.session import unit_of_work
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.workers.periodic import ENTRY_GRACE_S, reap_once
from app.workers.policy import POLICIES, lease_s
from app.workers.registry import JobContext, Outcome
from mkos_schemas.enums import JobKind
from procrastinate.manager import JobManager
from sqlalchemy.exc import OperationalError

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
from .test_job_retry_policy import _enqueue

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]


async def _image_job(tenant: uuid.UUID) -> tuple[uuid.UUID, str]:
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


def _age(jw: JobWorld, qid: str | int, seconds: int) -> None:
    """Move the entry's last fetch `seconds` into the past (no sleeping)."""
    jw.conn.execute(
        "UPDATE procrastinate_events SET at = at - make_interval(secs => %s) WHERE job_id = %s",
        [seconds, int(qid)],
    )


def _live_worker(jw: JobWorld) -> int:
    row = jw.conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now()) RETURNING id"
    ).fetchone()
    assert row is not None
    return int(row[0])


async def test_lost_finish_does_not_stall_the_image_lock(
    jobs_db: JobWorld, override_body: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[uuid.UUID] = []

    async def body(ctx: JobContext) -> Outcome:
        ran.append(ctx.job_run_id)
        return Outcome("succeeded")

    override_body(JobKind.IMAGE_COMPLIANCE, body)
    real_finish = JobManager.finish_job
    lost: list[int] = []

    async def finish_once_lost(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not lost:
            lost.append(1)
            raise OSError("server closed the connection unexpectedly")  # one queue-pool blip
        return await real_finish(self, *args, **kwargs)

    monkeypatch.setattr(JobManager, "finish_job", finish_once_lost)
    first, first_qid = await _image_job(jobs_db.tenant("acme"))
    async with running_worker(poll_s=0.2):
        await eventually(lambda: job_row(jobs_db.conn, first)["status"] == "succeeded")
        await eventually(lambda: bool(lost))
        assert queue_row(jobs_db.conn, first_qid)["status"] == "doing"  # the lock is held
        second, second_qid = await _image_job(jobs_db.tenant("bharat"))  # another tenant
        await asyncio.sleep(1.0)
        assert job_row(jobs_db.conn, second)["status"] == "queued"  # blocked by the lock
        fresh = await reap_once()
        assert fresh.retired_entries == []  # within the grace the worker may still finish it
        # the grace has passed (a terminal row is immutable, so the clock is not wound back:
        # the grace is shrunk instead, as the cap is in the JOBS-2 test)
        monkeypatch.setattr("app.workers.periodic.ENTRY_GRACE_S", 0)
        result = await reap_once()
        assert result.retired_entries == [int(first_qid)]
        await eventually(lambda: job_row(jobs_db.conn, second)["status"] == "succeeded")
    assert ran == [first, second]
    assert job_row(jobs_db.conn, first)["status"] == "succeeded"  # the row is untouched
    assert queue_row(jobs_db.conn, first_qid)["status"] == "failed"
    assert queue_row(jobs_db.conn, second_qid)["status"] == "succeeded"
    assert (await reap_once()).retired_entries == []


async def test_doing_entry_of_a_gone_row_is_retired(jobs_db: JobWorld) -> None:
    """A row removed by retention while its entry was still `doing` must not hold the lock."""
    job, qid = await _image_job(jobs_db.tenant("acme"))
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
        [_live_worker(jobs_db), int(qid)],
    )
    jobs_db.conn.execute("DELETE FROM job_runs WHERE id = %s", [job])
    assert (await reap_once()).retired_entries == []
    _age(jobs_db, qid, ENTRY_GRACE_S + 5)
    assert (await reap_once()).retired_entries == [int(qid)]


async def test_running_and_periodic_doing_entries_are_left_alone(jobs_db: JobWorld) -> None:
    job, qid = await _image_job(jobs_db.tenant("acme"))
    worker = _live_worker(jobs_db)
    jobs_db.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = 1, started_at = now(), "
        "lease_expires_at = now() + interval '1 hour' WHERE id = %s",
        [job],
    )
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
        [worker, int(qid)],
    )
    periodic = jobs_db.conn.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status, worker_id) "
        "VALUES ('default', 'reaper', '{\"timestamp\": 1}', 'doing', %s) RETURNING id",
        [worker],
    ).fetchone()
    assert periodic is not None
    _age(jobs_db, qid, 3600)
    _age(jobs_db, periodic[0], 3600)
    result = await reap_once()
    assert result.retired_entries == [] and result.redeferred == []
    assert queue_row(jobs_db.conn, qid)["status"] == "doing"
    assert queue_row(jobs_db.conn, periodic[0])["status"] == "doing"


async def test_lost_retry_under_a_live_worker_is_redeferred_past_the_lease(
    jobs_db: JobWorld,
) -> None:
    """A lost `retry_job`: the row was released to `queued`, but the entry stays `doing` under a
    worker that is still alive (never pruned). Past the kind's lease it gets a new entry."""
    job, qid = await _image_job(jobs_db.tenant("acme"))
    jobs_db.conn.execute("UPDATE job_runs SET attempt = 1 WHERE id = %s", [job])
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
        [_live_worker(jobs_db), int(qid)],
    )
    lease = lease_s(POLICIES[JobKind.IMAGE_COMPLIANCE])
    _age(jobs_db, qid, lease - 30)
    assert (await reap_once()).redeferred == []  # within the lease: a delivery may own it
    _age(jobs_db, qid, 60)
    result = await reap_once()
    assert result.redeferred == [job]
    row = job_row(jobs_db.conn, job)
    assert row["status"] == "queued" and row["queue_job_id"] != qid and row["attempt"] == 1
    assert queue_row(jobs_db.conn, qid)["status"] == "failed"
    fresh = jobs_db.conn.execute(
        "SELECT status, lock FROM procrastinate_jobs WHERE id = %s", [int(row["queue_job_id"])]
    ).fetchone()
    assert fresh == ("todo", "image_compliance")


async def test_infra_cap_ends_the_queued_row(
    jobs_db: JobWorld, override_body: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped path with the cap and backoff shrunk: the claim fails until the queue gives
    up, then the reaper ends the row so it matches the queue (never `queued` forever)."""
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(ctx.attempt)
        return Outcome("succeeded")

    override_body(JobKind.BULK_IMPORT, body)
    monkeypatch.setattr("app.workers.retry.INFRA_RETRY_CAP", 3)
    monkeypatch.setattr("app.workers.retry.backoff_s", lambda attempt, rand: 0.0)
    real_claim = JobRunRepository.claim
    outage = {"on": True}

    async def flaky(self: Any, *args: Any, **kwargs: Any) -> Any:
        if outage["on"]:
            raise OperationalError("UPDATE job_runs", {}, Exception("connection reset"))
        return await real_claim(self, *args, **kwargs)

    monkeypatch.setattr(JobRunRepository, "claim", flaky)
    job, qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    async with running_worker(poll_s=0.2):
        await eventually(lambda: queue_row(jobs_db.conn, qid)["status"] == "failed")
    outage["on"] = False
    assert job_row(jobs_db.conn, job)["status"] == "queued"
    result = await reap_once()
    assert result.dead_lettered == [job]
    row = job_row(jobs_db.conn, job)
    assert row["status"] == "dead_letter" and row["error_code"] == "INTERNAL_ERROR"
    assert row["finished_at"] is not None and row["queue_job_id"] == qid
    report = row["error_report_json"]
    assert report["error_code"] == "INTERNAL_ERROR" and len(report["errors"]) == 1
    assert (await reap_once()).dead_lettered == []  # idempotent
    assert runs == []
    assert count(jobs_db.conn, "SELECT count(*) FROM procrastinate_jobs WHERE status = 'todo'") == 0


@pytest.mark.parametrize("entry", ["cancelled", "succeeded", "gone"])
async def test_queued_row_with_a_final_or_missing_entry_is_ended(
    jobs_db: JobWorld, entry: str
) -> None:
    job, qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    if entry == "gone":
        jobs_db.conn.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [int(qid)])
    else:
        jobs_db.conn.execute(
            "UPDATE procrastinate_jobs SET status = %s WHERE id = %s", [entry, int(qid)]
        )
    result = await reap_once()
    assert result.dead_lettered == [job]
    assert job_row(jobs_db.conn, job)["status"] == "dead_letter"


async def test_stopped_tenant_lost_row_is_cancelled(jobs_db: JobWorld) -> None:
    job, qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'failed' WHERE id = %s", [int(qid)]
    )
    jobs_db.conn.execute(
        "UPDATE tenants SET status = 'suspended' WHERE id = %s", [jobs_db.tenant("acme")]
    )
    result = await reap_once()
    assert result.cancelled == [job] and result.dead_lettered == []
    assert job_row(jobs_db.conn, job)["status"] == "cancelled"
