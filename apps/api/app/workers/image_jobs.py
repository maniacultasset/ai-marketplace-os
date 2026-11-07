"""The `image_compliance` job body (SI7 T11, design §2.1): one `JobKind`, two bodies.

`JobKind` is a closed vocabulary (adding a member is MAJOR), so a candidate job reuses the
`image_compliance` kind with a `candidate_id` payload and the DB-only entity type
`image_candidate`. `run_image_job` routes it: a payload naming a `candidate_id` goes to
`run_image_candidate`, every other payload to the upload checks (`run_image_compliance`). The
dispatcher lives in `app.workers` because the candidate body is the one that may reach the AI
seam (`.importlinter`).
"""

from __future__ import annotations

from app.services.image_compliance import run_image_compliance
from app.workers.image_candidate import run_image_candidate
from app.workers.registry import JobContext, Outcome


async def run_image_job(ctx: JobContext) -> Outcome:
    if "candidate_id" in ctx.payload:
        return await run_image_candidate(ctx)
    return await run_image_compliance(ctx)
