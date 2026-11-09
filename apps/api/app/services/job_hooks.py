"""Per-kind job hooks, shared by the worker (`app/workers/wrapper.py`), the reaper and the API
cancel path (S13 PLAN §2; BACKEND-ARCHITECTURE:1718).

- `Hook(session, job, outcome)`: called once in the terminal transaction, after the terminal
  write, so `job.status`, `job.error_report_json` and `job.result_json` are final. `outcome` is
  the body's `Outcome` only on the wrapper's `succeeded`/`partial` path; on every other path
  (cancel, a non-running tenant's cancel, the reaper's cancel and dead-letter, timeout,
  `JobFailed`, retry exhaustion, any exception) it is None, so a hook branches on `job.status`.
- `RequeueHook(session, old, new)`: called in the staff requeue transaction (A15).

A hook that writes an entity row fences on the job (`... WHERE job_id = :this_job`), so an old
job's hook never overwrites a newer job's state. The steps that own a kind add to the maps:
S13b's `bulk_import` hooks (`app.services.imports.hooks`) are the first; S18 adds the
`listing_generate` move (`app.services.generation_hooks`); S21 adds the publish moves.

`session` is the terminal transaction's `AsyncSession`, typed `Any` here: repo rule R3 keeps
`sqlalchemy.ext.asyncio` out of `app/services`; hooks hand it to repositories only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from mkos_schemas.enums import JobKind

from app.db.models.ops import JobRun
from app.services.imports.hooks import import_requeue_hook, import_terminal_hook
from app.workers.registry import Outcome

Hook = Callable[[Any, JobRun, Outcome | None], Awaitable[None]]
RequeueHook = Callable[[Any, JobRun, JobRun], Awaitable[None]]

TERMINAL_HOOKS: Final[dict[JobKind, Hook]] = {JobKind.BULK_IMPORT: import_terminal_hook}
REQUEUE_HOOKS: Final[dict[JobKind, RequeueHook]] = {JobKind.BULK_IMPORT: import_requeue_hook}


def _register_owned_hooks() -> None:
    """Hooks of the kinds a merged step owns, present from import time (the API cancel path and
    the reaper need them as much as the worker does)."""
    from app.services.generation_hooks import on_generate_requeue, on_generate_terminal
    from app.services.image_candidate_hooks import on_image_requeue, on_image_terminal
    from app.services.publish_outcome import on_publish_requeue, on_publish_terminal
    from app.services.status_sync_outcome import refuse_requeue

    TERMINAL_HOOKS.setdefault(JobKind.BRAND_EMBED, on_embed_terminal)
    REQUEUE_HOOKS.setdefault(JobKind.BRAND_EMBED, on_embed_requeue)
    # S18 T17: a generation that ends without a version leaves `generating`; no requeue (CP5)
    TERMINAL_HOOKS.setdefault(JobKind.LISTING_GENERATE, on_generate_terminal)
    REQUEUE_HOOKS.setdefault(JobKind.LISTING_GENERATE, on_generate_requeue)
    # S21 T15: a publish job that ends without a verdict leaves `publishing`; no staff requeue
    TERMINAL_HOOKS.setdefault(JobKind.LISTING_PUBLISH, on_publish_terminal)
    REQUEUE_HOOKS.setdefault(JobKind.LISTING_PUBLISH, on_publish_requeue)
    # SI7 T11: a candidate job that ends without its image leaves `queued`; no staff requeue
    TERMINAL_HOOKS.setdefault(JobKind.IMAGE_COMPLIANCE, on_image_terminal)
    REQUEUE_HOOKS.setdefault(JobKind.IMAGE_COMPLIANCE, on_image_requeue)
    # SI11 T7: one kind, two bodies (the status read and the marketplace dry run)
    TERMINAL_HOOKS.setdefault(JobKind.LISTING_STATUS_SYNC, on_sync_terminal)
    REQUEUE_HOOKS.setdefault(JobKind.LISTING_STATUS_SYNC, refuse_requeue)


async def on_embed_terminal(session: Any, job: JobRun, outcome: Outcome | None) -> None:
    """SI13 (CP-D1): a diagnosis job (`entity_type 'error_proposal'`) fails its pending proposal
    when it dies; any other `brand_embed` job keeps the brand document hook."""
    from app.services.brand_embed import on_brand_embed_terminal
    from app.workers.error_diagnosis import on_proposal_terminal

    if job.entity_type == "error_proposal":
        await on_proposal_terminal(session, job)
        return
    await on_brand_embed_terminal(session, job, outcome)


async def on_embed_requeue(session: Any, old: JobRun, new: JobRun) -> None:
    """A diagnosis job is never requeued by staff (the scheduler claims a retry itself)."""
    from app.services.brand_embed import on_brand_embed_requeue

    if old.entity_type == "error_proposal":
        from app.repositories.jobs import NotEligibleError

        raise NotEligibleError("the diagnosis scheduler retries a proposal")
    await on_brand_embed_requeue(session, old, new)


async def on_sync_terminal(session: Any, job: JobRun, outcome: Outcome | None) -> None:
    """The `listing_status_sync` terminal hook (SI11 T7): a dry run's (`op = preview`) own end,
    never a counted poll (CP-D16); a status read's failed-poll count as before. After every
    terminal of the kind, a check left `pending` behind this job gets its own job (CP-D5)."""
    from app.services import remote_preview
    from app.services.status_sync_outcome import count_failed_poll

    if remote_preview.is_preview(job):
        await remote_preview.on_terminal(session, job, outcome)
    else:
        await count_failed_poll(session, job, outcome)
        if job.status == "succeeded":
            await _read_entry(session, job)
    await remote_preview.enqueue_pending(session, job)


async def _read_entry(session: Any, job: JobRun) -> None:
    """SI11 T13: a status read that left adapter issues open on a `failed` (rejected) or
    `published` (BUYABLE with ERROR issues) listing runs the resolution engine; so does one the
    read moved to `blocked/marketplace_action_required` (T18: the marketplace lane opens the
    Seller Central step; it never enqueues a job)."""
    from app.repositories.uow import repositories_on
    from app.services.resolution.run import resolve

    if job.listing_id is None:
        return
    uow = repositories_on(session, job.tenant_id)
    listing = await uow.listings.get(job.listing_id)
    if listing is not None and (
        listing.status in ("failed", "published")
        or (listing.status == "blocked" and listing.status_reason == "marketplace_action_required")
    ):
        await resolve(uow, listing, entry="read", job=job)


async def run_terminal_hook(
    session: Any, job: JobRun, outcome: Outcome | None, hooks: Mapping[JobKind, Hook] | None = None
) -> None:
    """The kind's terminal hook, if any, in the caller's terminal transaction."""
    hook = (TERMINAL_HOOKS if hooks is None else hooks).get(JobKind(job.kind))
    if hook is not None:
        await hook(session, job, outcome)


async def run_requeue_hook(session: Any, old: JobRun, new: JobRun) -> None:
    hook = REQUEUE_HOOKS.get(JobKind(old.kind))
    if hook is not None:
        await hook(session, old, new)


_register_owned_hooks()
