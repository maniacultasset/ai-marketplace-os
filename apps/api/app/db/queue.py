"""The queue plumbing (ADR-0004 §2; DATABASE.md:952; spike REPORT §2 C1, §5 item 2).

procrastinate's tables live in the same database as `job_runs`, so a job is deferred on the raw
psycopg connection of the caller's SQLAlchemy transaction: the `job_runs` row and its queue
entry commit or roll back together, and nothing sweeps a commit gap. The API never opens the
procrastinate pool (`get_queue_app()` is only built for its connector and task names); only the
worker process opens it (`app.workers.main`).

A queue entry carries exactly `{"job_run_id": "<uuid>"}` (DATABASE.md:941), never tenant data,
which is why the procrastinate tables need no RLS. `job_runs` stays the source of truth: the
API reads job state from it, never from the queue.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Final
from uuid import UUID

import procrastinate
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_engine

QUEUES: Final = ("default", "publish")
# The worker's procrastinate pool (ADR-0001:44: "Worker: 2 x (app pool 10 + procrastinate pool 5)").
QUEUE_POOL_MIN_SIZE: Final = 1
QUEUE_POOL_MAX_SIZE: Final = 5
# Finishes a dead entry (its worker was killed) so it never runs again (the reaper, A9). A
# `todo` entry also qualifies: it is a stale copy of a job already re-deferred.
_RETIRE: Final = text(
    "UPDATE procrastinate_jobs SET status = 'failed', abort_requested = false "
    "WHERE id = :id AND status IN ('todo', 'doing')"
)


# Audit JOBS-4: the `doing` entries of `job_runs` jobs (a periodic task's entry carries no
# `job_run_id`), with the seconds since each was last fetched (its newest `started` event), for
# the reaper's sweep of entries whose job already ended. Read and written on an engine connection
# as `app_rw`: the queue tables have no tenant column and no RLS, and `app_platform_ro` has no
# grant on them. Only queue ids, job ids and ages are read, never a payload.
_DOING_JOB_ENTRIES: Final = text(
    "SELECT j.id, j.args->>'job_run_id', EXTRACT(EPOCH FROM now() - ("
    "SELECT max(e.at) FROM procrastinate_events e WHERE e.job_id = j.id AND e.type = 'started'"
    ")) FROM procrastinate_jobs j WHERE j.status = 'doing' AND j.args ? 'job_run_id' "
    "ORDER BY j.id LIMIT :limit"
)
_RETIRE_DOING: Final = text(
    "UPDATE procrastinate_jobs SET status = 'failed', abort_requested = false "
    "WHERE id = ANY(:ids) AND status = 'doing' RETURNING id"
)


def libpq_conninfo(database_url: str) -> str:
    """The SQLAlchemy URL as a libpq URI (`postgresql+psycopg://` → `postgresql://`)."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def new_queue_app() -> procrastinate.App:
    """A procrastinate app on DATABASE_URL (`app_rw`); building it opens no connection."""
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATABASE_URL is required to build the queue app")
    connector = procrastinate.PsycopgConnector(
        conninfo=libpq_conninfo(settings.database_url),
        min_size=QUEUE_POOL_MIN_SIZE,
        max_size=QUEUE_POOL_MAX_SIZE,
    )
    return procrastinate.App(connector=connector)


@lru_cache(maxsize=1)
def get_queue_app() -> procrastinate.App:
    """The process's queue app, built lazily. Opening its pool is the worker's job alone."""
    return new_queue_app()


async def _driver_connection(session: AsyncSession) -> Any:
    """The psycopg `AsyncConnection` under the session's current transaction."""
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    return raw.driver_connection


async def defer_on_session(
    session: AsyncSession, *, kind: str, queue: str, job_run_id: UUID, lock: str | None = None
) -> str:
    """Defer task `kind` on `queue` with args `{"job_run_id": "<id>"}` inside the session's
    transaction; returns the queue job id (as text, for `job_runs.queue_job_id`). `lock` is the
    procrastinate lock (S17b: one `image_compliance` job runs at a time)."""
    if queue not in QUEUES:
        raise ValueError(f"unknown queue {queue!r}")
    if not isinstance(job_run_id, UUID):
        raise ValueError("job_run_id must be a UUID")
    driver = await _driver_connection(session)
    deferrer = get_queue_app().configure_task(name=kind, queue=queue, connection=driver, lock=lock)
    queue_job_id = await deferrer.defer_async(job_run_id=str(job_run_id))
    return str(queue_job_id)


async def cancel_on_session(session: AsyncSession, queue_job_id: str) -> bool:
    """Cancel a `todo` queue entry in the session's transaction. A `doing` entry is never
    touched (False): a running job is not cancellable (BACKEND-ARCHITECTURE:1718)."""
    driver = await _driver_connection(session)
    return await get_queue_app().job_manager.cancel_job_by_id_async(
        int(queue_job_id), abort=False, connection=driver
    )


async def retire_on_session(session: AsyncSession, queue_job_id: str) -> None:
    """Finish a dead `doing` (or stale `todo`) entry as `failed` in the session's transaction,
    so a re-deferred job's old entry can never run."""
    await session.execute(_RETIRE, {"id": int(queue_job_id)})


async def doing_job_entries(*, limit: int = 500) -> list[tuple[int, str | None, float | None]]:
    """`(queue job id, job_run_id arg, seconds since fetched)` of every `doing` job entry, oldest
    first (the reaper's lost-finish sweep, audit JOBS-4). Not tenant data: ids and ages only."""
    async with get_engine().connect() as connection:
        rows = (await connection.execute(_DOING_JOB_ENTRIES, {"limit": limit})).all()
    return [(int(r[0]), r[1], None if r[2] is None else float(r[2])) for r in rows]


async def retire_doing(queue_job_ids: list[int]) -> list[int]:
    """Finish the given entries as `failed` if they are still `doing`; the ids retired. Their
    jobs already ended in `job_runs`, so this only releases the entry (and its lock)."""
    if not queue_job_ids:
        return []
    async with get_engine().begin() as connection:
        rows = (await connection.execute(_RETIRE_DOING, {"ids": queue_job_ids})).all()
    return [int(r[0]) for r in rows]
