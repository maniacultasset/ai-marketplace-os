"""One procrastinate task per `JobKind` (task name = the kind value, queue from the policy), each
calling the wrapper,
with the one retry strategy (`app.workers.retry`). The API defers by name (`app.db.queue.defer_on_session`) and never imports
this module; the worker registers the tasks on the queue app at start-up."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import procrastinate

from app.workers.policy import POLICIES
from app.workers.retry import JobRetryStrategy
from app.workers.wrapper import run_job


def _task_body() -> Callable[[str], Awaitable[None]]:
    async def task(job_run_id: str) -> None:
        await run_job(job_run_id)

    return task


def register_tasks(app: procrastinate.App) -> None:
    """Register the six kind tasks on `app`; a second call is a no-op."""
    for kind, policy in POLICIES.items():
        if kind.value in app.tasks:
            continue
        app.task(name=kind.value, queue=policy.queue, pass_context=False, retry=JobRetryStrategy())(
            _task_body()
        )
