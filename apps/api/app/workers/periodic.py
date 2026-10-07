"""Periodic tasks (S13 PLAN D4, D12; DATABASE.md:952, :1028), registered on the queue app by the
worker. procrastinate's periodic deferrer runs in every worker, and `procrastinate_periodic_defers`
plus a `queueing_lock` per task make each schedule slot fire once across all workers. A periodic
queue entry carries only `{"timestamp"}`, never tenant data.

- `reaper` (every minute): `reap_once()`. It lists expired `running` rows through
  `platform_scope()` (the read-only cross-tenant session; repo rule R5 allows this module), then
  handles each job in its own tenant's `unit_of_work`: a stopped tenant's job is cancelled, one
  with attempts left is re-queued and re-deferred, an exhausted one is `dead_letter` +
  `JOB_TIMEOUT`; the dead queue entry is retired and the kind's terminal hook runs for the two
  terminal moves. Then stale worker rows are pruned (spike REPORT §4 gotcha 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

import procrastinate
from mkos_schemas.codes import ErrorCode

from app.core.logging import get_logger
from app.core.request_context import new_request_id, request_context
from app.db.session import platform_scope, unit_of_work
from app.repositories.jobs import JobRunRepository, expired_running
from app.services.job_hooks import run_terminal_hook
from app.workers.errors import system_report
from app.workers.settings import STALLED_WORKER_S

log = get_logger(__name__)
REAPER_ID: Final = "reaper"


@dataclass
class ReapResult:
    requeued: list[UUID] = field(default_factory=list)
    dead_lettered: list[UUID] = field(default_factory=list)
    cancelled: list[UUID] = field(default_factory=list)
    skipped: list[UUID] = field(default_factory=list)
    pruned_workers: list[int] = field(default_factory=list)


def _timeout_report(job: Any, request_id: str) -> dict[str, Any]:
    report = system_report(
        ErrorCode.JOB_TIMEOUT,
        "the job's lease expired with no attempts left (its worker stopped)",
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        request_id=job.request_id or request_id,
    )
    return report.model_dump(mode="json")


async def reap_once(job_manager: Any | None = None) -> ReapResult:
    """One reaper pass. `job_manager` (the open queue app's) prunes stale worker rows."""
    result = ReapResult()
    with request_context(new_request_id()) as ctx:
        async with platform_scope() as session:
            expired = await expired_running(session)
        for job_id, tenant_id in expired:
            async with unit_of_work(tenant_id) as session:
                reaped = await JobRunRepository(session, tenant_id).reap(
                    job_id, lambda job: _timeout_report(job, ctx.request_id)
                )
                if reaped.action in ("dead_letter", "cancelled") and reaped.job is not None:
                    await run_terminal_hook(session, reaped.job, None)
            bucket = {
                "requeued": result.requeued,
                "dead_letter": result.dead_lettered,
                "cancelled": result.cancelled,
            }.get(reaped.action, result.skipped)
            bucket.append(job_id)
            log.info("job.reaped", job_run_id=str(job_id), action=reaped.action)
        if job_manager is not None:
            result.pruned_workers = list(await job_manager.prune_stalled_workers(STALLED_WORKER_S))
    return result


async def _reaper(context: procrastinate.JobContext, timestamp: int) -> None:
    await reap_once(context.app.job_manager)


def register_periodic(app: procrastinate.App) -> None:
    """Register the periodic tasks on `app`; a second call is a no-op."""
    name = f"periodic_{REAPER_ID}"
    if name not in app.tasks:
        task = app.task(name=name, queue="default", pass_context=True)(_reaper)
        app.periodic(cron="* * * * *", periodic_id=REAPER_ID, queueing_lock=REAPER_ID)(task)
