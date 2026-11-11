"""The `image_compliance` job body (SI7 T11, design §2.1): one `JobKind`, two bodies.

`JobKind` is a closed vocabulary (adding a member is MAJOR), so a candidate job reuses the
`image_compliance` kind with a `candidate_id` payload and the DB-only entity type
`image_candidate`. `run_image_job` routes it: a payload naming a `candidate_id` goes to
`run_image_candidate`, SI12's `size_chart_read` payload to `run_size_chart_read`, SI14's
`category_suggestion` payload to `run_category_suggestion` (a `category_draft` one, the
suggestion before a product is saved, to `run_category_draft`) and `column_map` to `run_column_map`,
V1.1's `photo_facts_read` (the photo read on upload) to `run_photo_facts_read`,
every other payload to the upload
checks (`run_image_compliance`). The
dispatcher lives in `app.workers` because the candidate body is the one that may reach the AI
seam (`.importlinter`).
"""

from __future__ import annotations

from app.services.image_compliance import run_image_compliance
from app.workers.category_draft import run_category_draft
from app.workers.category_suggestion import run_category_suggestion
from app.workers.column_map import run_column_map
from app.workers.image_candidate import run_image_candidate
from app.workers.photo_facts_read import run_photo_facts_read
from app.workers.policy import CATEGORY_DRAFT_PAYLOAD
from app.workers.registry import JobContext, Outcome
from app.workers.size_chart_read import run_size_chart_read


async def run_image_job(ctx: JobContext) -> Outcome:
    if CATEGORY_DRAFT_PAYLOAD in ctx.payload:
        return await run_category_draft(ctx)
    if "category_suggestion" in ctx.payload:
        return await run_category_suggestion(ctx)
    if "column_map" in ctx.payload:
        return await run_column_map(ctx)
    if "photo_facts_read" in ctx.payload:
        return await run_photo_facts_read(ctx)
    if "size_chart_read" in ctx.payload:
        return await run_size_chart_read(ctx)
    if "candidate_id" in ctx.payload:
        return await run_image_candidate(ctx)
    return await run_image_compliance(ctx)
