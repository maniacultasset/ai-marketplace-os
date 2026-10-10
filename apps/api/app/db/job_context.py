"""The worker's first read (DATABASE.md:950, §11.2 row 19; spike REPORT §5 item 3).

A queue entry carries only `{"job_run_id"}`, so before any tenant GUC exists the worker names
the job: one short transaction sets the transaction-local `app.job_id` GUC and reads the row's
`tenant_id`, `kind`, `request_id` and (S23) `traceparent` through the `jr_by_id` policy (SELECT where
`id = app_job_id()`), the named invariant-5 exception. Nothing else is visible to it, and the
GUC is gone at COMMIT. Every later statement of the job runs in `unit_of_work(tenant_id)`.

`bind_job_actor(session, job_id)` / `unbind_job_actor(session)` (S18 T5) set and clear the same
transaction-local GUC around one listing move, so `listings_status_guard` sees the worker actor
(`app_job_id()`) for that statement only.

Repo rule R8: this is the only module that names the GUC, and only `app/workers/wrapper.py` and
`app/repositories/listings.py` import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from app.db.guard import SCOPE_OK
from app.db.models.ops import JobRun
from app.db.session import get_sessionmaker


@dataclass(frozen=True, slots=True)
class JobHead:
    tenant_id: UUID
    kind: str
    request_id: str | None
    traceparent: str | None = None  # S23: the enqueuing request's trace context


async def resolve_job(job_run_id: UUID) -> JobHead | None:
    """The job's tenant, kind and origin request id, or None when no visible row has that id."""
    if not isinstance(job_run_id, UUID):
        raise ValueError("job_run_id must be a UUID")
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(select(func.set_config("app.job_id", str(job_run_id), True)))
        stmt = (
            select(JobRun.tenant_id, JobRun.kind, JobRun.request_id, JobRun.traceparent)
            .where(JobRun.id == job_run_id)
            .execution_options(**{SCOPE_OK: True})
        )
        row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return JobHead(
        tenant_id=row.tenant_id,
        kind=row.kind,
        request_id=row.request_id,
        traceparent=row.traceparent,
    )


async def bind_job_actor(session: Any, job_id: UUID) -> None:
    """Name `job_id` as the actor of the next statements in `session`'s transaction."""
    if not isinstance(job_id, UUID):
        raise ValueError("job_id must be a UUID")
    await session.execute(select(func.set_config("app.job_id", str(job_id), True)))


async def unbind_job_actor(session: Any) -> None:
    """Clear the worker actor again (the GUC reads as NULL, `app_job_id()`)."""
    await session.execute(select(func.set_config("app.job_id", "", True)))
