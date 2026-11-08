"""The `listing_status_sync` job body (SI11 T7, P:D4): one `JobKind`, two bodies.

`JobKind` is a closed vocabulary (adding a member is MAJOR), so the marketplace dry run reuses
the sync kind with `op = "preview"` in its payload (the SI7 precedent, `app/workers/image_jobs.py`).
`run_sync_job` routes it: a preview payload goes to `run_remote_preview`, every other payload to
the status read (`run_listing_status_sync`).
"""

from __future__ import annotations

from app.workers.registry import JobContext, Outcome
from app.workers.remote_preview import run_remote_preview
from app.workers.status_sync import run_listing_status_sync


async def run_sync_job(ctx: JobContext) -> Outcome:
    if ctx.payload.get("op") == "preview":
        return await run_remote_preview(ctx)
    return await run_listing_status_sync(ctx)
