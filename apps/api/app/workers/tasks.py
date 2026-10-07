"""One procrastinate task per `JobKind` (task name = the kind value, queue from the policy), each
calling the wrapper,
with the one retry strategy (`app.workers.retry`). The API defers by name (`app.db.queue.defer_on_session`) and never imports
this module; the worker registers the tasks on the queue app at start-up."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import procrastinate
from mkos_schemas.enums import JobKind

from app.services.imports.runner import run_import
from app.workers import registry
from app.workers.policy import POLICIES
from app.workers.retry import JobRetryStrategy
from app.workers.wrapper import run_job


def _task_body() -> Callable[[str], Awaitable[None]]:
    async def task(job_run_id: str) -> None:
        await run_job(job_run_id)

    return task


def register_bodies() -> None:
    """The job bodies the merged steps own (S13b `bulk_import`, S16 `brand_embed`); a body a
    test installed first is kept."""
    from app.services.brand_embed import run_brand_embed

    if registry.body_for(JobKind.BULK_IMPORT) is None:
        registry.register(JobKind.BULK_IMPORT, run_import)
    if registry.body_for(JobKind.BRAND_EMBED) is None:
        registry.register(JobKind.BRAND_EMBED, run_brand_embed)


def register_tasks(app: procrastinate.App) -> None:
    """Register the job bodies and the six kind tasks on `app`; a second call is a no-op."""
    register_bodies()
    for kind, policy in POLICIES.items():
        if kind.value in app.tasks:
            continue
        app.task(name=kind.value, queue=policy.queue, pass_context=False, retry=JobRetryStrategy())(
            _task_body()
        )
