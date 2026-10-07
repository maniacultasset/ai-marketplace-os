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
- `prune_rate_limits` (every 5 min): counters whose window started over 5 minutes ago.
- `prune_idempotency_keys` (hourly): expired HTTP keys, tenant by tenant.
- `expire_trials` (hourly): `expire_trials()` (trials past their end → `suspended`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import procrastinate
from mkos_schemas.codes import ErrorCode

from app.core.logging import get_logger
from app.core.request_context import new_request_id, request_context
from app.db.session import platform_scope, unit_of_work
from app.repositories import rate_limits, retention
from app.repositories.idempotency import IdempotencyKeyRepository
from app.repositories.jobs import JobRunRepository, expired_running
from app.services.job_hooks import run_terminal_hook
from app.workers.errors import system_report
from app.workers.settings import STALLED_WORKER_S

log = get_logger(__name__)
REAPER_ID: Final = "reaper"
RATE_LIMIT_KEEP: Final = timedelta(minutes=5)


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


async def prune_rate_limits(now: datetime | None = None) -> int:
    pruned = await rate_limits.prune((now or datetime.now(UTC)) - RATE_LIMIT_KEEP)
    log.info("periodic.rate_limits_pruned", rows=pruned)
    return pruned


async def prune_idempotency_keys() -> int:
    async with platform_scope() as session:
        tenants = await retention.tenant_ids(session)
    total = 0
    for tenant_id in tenants:
        async with unit_of_work(tenant_id) as session:
            total += await IdempotencyKeyRepository(session, tenant_id).prune_expired()
    log.info("periodic.idempotency_keys_pruned", rows=total)
    return total


async def run_expire_trials() -> int:
    expired = await retention.expire_trials()
    log.info("periodic.trials_expired", tenants=expired)
    return expired


async def _reaper(context: procrastinate.JobContext, timestamp: int) -> None:
    await reap_once(context.app.job_manager)


def _plain(fn: Callable[[], Awaitable[Any]]) -> Callable[[int], Awaitable[None]]:
    async def task(timestamp: int) -> None:
        with request_context(new_request_id()):
            await fn()

    return task


# periodic_id -> (cron, the task function, pass_context); cron fields: minute hour dom month dow
PERIODIC: Final[dict[str, tuple[str, Any, bool]]] = {
    REAPER_ID: ("* * * * *", _reaper, True),
    "prune_rate_limits": ("*/5 * * * *", _plain(prune_rate_limits), False),
    "prune_idempotency_keys": ("0 * * * *", _plain(prune_idempotency_keys), False),
    "expire_trials": ("15 * * * *", _plain(run_expire_trials), False),
}


def register_periodic(app: procrastinate.App) -> None:
    """Register every periodic task on `app` (queue `default`, `queueing_lock` = its id, so a
    slot never queues twice); a second call is a no-op."""
    for periodic_id, (cron, fn, pass_context) in PERIODIC.items():
        name = f"periodic_{periodic_id}"
        if name in app.tasks:
            continue
        if pass_context:
            task: Any = app.task(name=name, queue="default", pass_context=True)(fn)
        else:
            task = app.task(name=name, queue="default", pass_context=False)(fn)
        app.periodic(cron=cron, periodic_id=periodic_id, queueing_lock=periodic_id)(task)
