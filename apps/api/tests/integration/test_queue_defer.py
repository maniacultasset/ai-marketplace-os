"""S13 A2 (AC1; spike REPORT C1): defer and cancel on the caller's session, as app_rw."""

from __future__ import annotations

import pytest
from app.db.queue import cancel_on_session, defer_on_session, get_queue_app, retire_on_session
from app.db.session import unit_of_work

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, count, insert_job, jobs_db, queue_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]


async def _defer(jw: JobWorld, slug: str = "acme") -> str:
    tenant = jw.tenant(slug)
    job_id = insert_job(jw.conn, tenant)
    async with unit_of_work(tenant) as session:
        return await defer_on_session(
            session, kind="bulk_import", queue="default", job_run_id=job_id
        )


async def test_defer_args_exactly_job_run_id(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    job_id = insert_job(jobs_db.conn, tenant)
    async with unit_of_work(tenant) as session:
        qid = await defer_on_session(
            session, kind="bulk_import", queue="default", job_run_id=job_id
        )
    row = queue_row(jobs_db.conn, qid)
    assert row["args"] == {"job_run_id": str(job_id)}
    assert set(row["args"]) == {"job_run_id"}
    assert (row["queue_name"], row["task_name"], row["status"]) == (
        "default",
        "bulk_import",
        "todo",
    )


async def test_defer_rolls_back_with_session(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    job_id = insert_job(jobs_db.conn, tenant)

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):
        async with unit_of_work(tenant) as session:
            await defer_on_session(session, kind="bulk_import", queue="default", job_run_id=job_id)
            raise BoomError
    assert count(jobs_db.conn, "SELECT count(*) FROM procrastinate_jobs") == 0


async def test_defer_refuses_unknown_queue(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    job_id = insert_job(jobs_db.conn, tenant)
    with pytest.raises(ValueError, match="unknown queue"):
        async with unit_of_work(tenant) as session:
            await defer_on_session(session, kind="bulk_import", queue="nope", job_run_id=job_id)


async def test_api_never_opens_queue_pool(jobs_db: JobWorld) -> None:
    await _defer(jobs_db)
    connector = get_queue_app().connector
    assert connector._async_pool is None  # type: ignore[attr-defined]
    assert count(jobs_db.conn, "SELECT count(*) FROM procrastinate_jobs") == 1


async def test_cancel_todo_only(jobs_db: JobWorld) -> None:
    todo = await _defer(jobs_db)
    doing = await _defer(jobs_db)
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", [int(doing)]
    )
    tenant = jobs_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        assert await cancel_on_session(session, todo) is True
        assert await cancel_on_session(session, doing) is False
    assert queue_row(jobs_db.conn, todo)["status"] == "cancelled"
    assert queue_row(jobs_db.conn, doing)["status"] == "doing"


async def test_retire_marks_failed(jobs_db: JobWorld) -> None:
    qid = await _defer(jobs_db)
    jobs_db.conn.execute("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", [int(qid)])
    async with unit_of_work(jobs_db.tenant("acme")) as session:
        await retire_on_session(session, qid)
    assert queue_row(jobs_db.conn, qid)["status"] == "failed"
