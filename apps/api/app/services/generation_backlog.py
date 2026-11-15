"""B19: the per-tenant backlog of generations past the live cap.

`GENERATION_LIVE_JOBS_PER_TENANT` bounds how many `listing_generate` jobs one tenant holds on the
shared `default` queue (4 slots, FIFO), so one brand's bulk send never starves the others. Past it
a generate or regenerate is no longer refused: the job is written waiting (`queued`, no queue entry
yet; `app.repositories.jobs`), the 202 says so (`waiting_for_slot`), and it is promoted (given its
queue entry) when a slot frees, oldest first, under the tenant's generate lock:

- `promote(jobs)`: the caller holds the lock; fills `cap - live` slots with the oldest waiting
  jobs. Every generate and regenerate request runs it after writing its own job, so a new request
  never overtakes an older waiting one.
- `promote_on_terminal(session, job)`: the `listing_generate` terminal hook's last step (every
  end: success, failure, timeout, cancel, the reaper's). The terminal transaction already holds the
  job's and its listing's rows, so it only tries the lock (a request holding it may be waiting for
  that listing) and runs in a savepoint: a failure is logged and never undoes the terminal write.
- `promote_tenant(tenant_id)`: its own transaction, waiting for the lock (the caller holds no
  other). The worker runs it right after a generation's terminal commit, so two generations that
  end together (one hook skipped for the other's lock) still fill both slots; the reaper's pass
  (every minute, `app.workers.periodic`) runs it for every tenant holding a waiting job, so a
  promotion lost to a crash waits at most a minute. It never raises.

A promoted job is claimed like any other: the claim's budget reservation refuses it with
`BUDGET_EXCEEDED` (or the AI pause's code) when the day's budget is spent, which fails the job and
its listing with the seller-facing reason, as for a job queued before the budget ran out. The
backlog is bounded by the catalogue: a listing holds at most one live generation.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.ops import JobRun
from app.repositories.jobs import JobRunRepository
from app.repositories.transactions import unit_of_work

log = get_logger(__name__)


async def promote(jobs: JobRunRepository) -> list[uuid.UUID]:
    """The tenant's free slots filled with its oldest waiting generations (the caller holds the
    tenant's generate lock); the ids promoted."""
    free = get_settings().generation_live_jobs_per_tenant - await jobs.count_live_generations()
    promoted = await jobs.promote_waiting_generations(free)
    for job in promoted:
        log.info("generation.promoted", job_run_id=str(job.id))
    return [job.id for job in promoted]


async def promote_on_terminal(session: Any, job: JobRun) -> None:
    """The terminal hook's promotion (module docstring); never raises."""
    jobs = JobRunRepository(session, job.tenant_id)
    try:
        async with session.begin_nested():
            if not await jobs.try_lock_generate_enqueue():
                log.info("generation.promote_skipped", job_run_id=str(job.id))
                return
            await promote(jobs)
    except Exception as exc:  # the reaper's pass promotes instead
        log.warning(
            "generation.promote_failed", job_run_id=str(job.id), error_class=type(exc).__name__
        )


async def promote_tenant(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """The tenant's free slots filled in a transaction of its own, under the generate lock
    (module docstring); the ids promoted, or none when it failed (logged)."""
    try:
        async with unit_of_work(tenant_id) as session:
            jobs = JobRunRepository(session, tenant_id)
            await jobs.lock_generate_enqueue()
            return await promote(jobs)
    except Exception as exc:  # the next end or the reaper's pass promotes instead
        log.warning(
            "generation.promote_failed", tenant_id=str(tenant_id), error_class=type(exc).__name__
        )
        return []
