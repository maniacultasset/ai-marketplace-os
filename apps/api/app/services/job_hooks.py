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
S13b's `bulk_import` hooks (`app.services.imports.hooks`) are the first; S18 and S21 add the
listing moves.

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
    from app.services.brand_embed import on_brand_embed_requeue, on_brand_embed_terminal

    TERMINAL_HOOKS.setdefault(JobKind.BRAND_EMBED, on_brand_embed_terminal)
    REQUEUE_HOOKS.setdefault(JobKind.BRAND_EMBED, on_brand_embed_requeue)


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
