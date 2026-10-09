"""One procrastinate task per `JobKind` (task name = the kind value, queue from the policy), each
calling the wrapper,
with the one retry strategy (`app.workers.retry`). The API defers by name (`app.db.queue.defer_on_session`) and never imports
this module; the worker registers the tasks on the queue app at start-up."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import procrastinate
from mkos_schemas.enums import JobKind

from app.services.image_compliance import run_image_compliance
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
    """The job bodies the steps own (S13b `bulk_import`, S16 `brand_embed`, S17b
    `image_compliance`, S18 `listing_generate`, S21 `listing_publish`); a body already set is kept (tests install fakes
    first)."""
    from app.services.brand_embed import run_brand_embed
    from app.workers.generation import run_listing_generate
    from app.workers.publish import run_listing_publish

    if registry.body_for(JobKind.BULK_IMPORT) is None:
        registry.register(JobKind.BULK_IMPORT, run_import)
    if registry.body_for(JobKind.BRAND_EMBED) is None:
        registry.register(JobKind.BRAND_EMBED, run_brand_embed)
    if registry.body_for(JobKind.IMAGE_COMPLIANCE) is None:
        registry.register(JobKind.IMAGE_COMPLIANCE, run_image_compliance)
    if registry.body_for(JobKind.LISTING_GENERATE) is None:
        registry.register(JobKind.LISTING_GENERATE, run_listing_generate)
    if registry.body_for(JobKind.LISTING_PUBLISH) is None:
        registry.register(JobKind.LISTING_PUBLISH, run_listing_publish)


def register_tasks(app: procrastinate.App) -> None:
    """Register the job bodies and the six kind tasks on `app`; a second call is a no-op."""
    register_bodies()
    for kind, policy in POLICIES.items():
        if kind.value in app.tasks:
            continue
        app.task(name=kind.value, queue=policy.queue, pass_context=False, retry=JobRetryStrategy())(
            _task_body()
        )
