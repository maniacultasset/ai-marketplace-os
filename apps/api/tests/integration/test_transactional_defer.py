"""S13 A3 (DW1, AC1): the one enqueue helper writes `job_runs` and the queue entry in the
caller's transaction: invisible inside it, gone on rollback, both there on commit."""

from __future__ import annotations

import uuid

import pytest
from app.db.session import unit_of_work
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from mkos_schemas.enums import JobKind
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, count, jobs_db, queue_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]


def _spec(jw: JobWorld, **extra: object) -> EnqueueSpec:
    return EnqueueSpec(
        kind=JobKind.BULK_IMPORT,
        entity_type="import",
        entity_id=uuid.uuid4(),
        created_by=jw.owner("acme"),
        **extra,  # type: ignore[arg-type]
    )


def _counts(jw: JobWorld) -> tuple[int, int]:
    return (
        count(jw.conn, "SELECT count(*) FROM job_runs"),
        count(jw.conn, "SELECT count(*) FROM procrastinate_jobs"),
    )


class RollbackError(Exception):
    pass


async def test_transactional_defer(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    with pytest.raises(RollbackError):
        async with unit_of_work(tenant) as session:
            await JobRunRepository(session, tenant).enqueue(_spec(jobs_db))
            assert _counts(jobs_db) == (0, 0)  # another session sees nothing yet
            raise RollbackError
    assert _counts(jobs_db) == (0, 0)

    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(_spec(jobs_db))
        assert _counts(jobs_db) == (0, 0)
    assert done.created is True
    assert _counts(jobs_db) == (1, 1)
    stored = jobs_db.conn.execute(
        "SELECT queue_job_id, status, attempt, max_attempts, queue FROM job_runs WHERE id = %s",
        [done.job.id],
    ).fetchone()
    assert stored is not None
    qid = jobs_db.conn.execute("SELECT id FROM procrastinate_jobs").fetchone()
    assert qid is not None
    assert stored == (str(qid[0]), "queued", 0, 3, "default")
    assert done.job.queue_job_id == str(qid[0])


async def test_transactional_defer_later_statement_fails(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    with pytest.raises(IntegrityError, match="entity_id"):
        async with unit_of_work(tenant) as session:
            await JobRunRepository(session, tenant).enqueue(_spec(jobs_db))
            # a later statement of the same transaction violates NOT NULL (entity_id)
            await session.execute(
                text(
                    "INSERT INTO job_runs (tenant_id, kind, entity_type, payload_json, "
                    "correlation_id) VALUES (:t, 'bulk_import', 'import', '{}', gen_random_uuid())"
                ),
                {"t": tenant},
            )
    assert _counts(jobs_db) == (0, 0)


async def test_transactional_defer_args_exactly_job_run_id(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(_spec(jobs_db))
    assert done.job.queue_job_id is not None
    row = queue_row(jobs_db.conn, done.job.queue_job_id)
    assert row["args"] == {"job_run_id": str(done.job.id)}
    assert row["task_name"] == "bulk_import"
