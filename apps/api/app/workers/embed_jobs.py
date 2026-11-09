"""The `brand_embed` kind's two bodies (SI13 T12; CP-D1): an error diagnosis when the payload says
`op: diagnose` (`app.workers.error_diagnosis`), else the brand document or profile embedding."""

from __future__ import annotations

from app.workers.registry import JobContext, Outcome


async def run_embed_job(ctx: JobContext) -> Outcome:
    if ctx.payload.get("op") == "diagnose":
        from app.workers.error_diagnosis import run_error_diagnosis

        return await run_error_diagnosis(ctx)
    from app.services.brand_embed import run_brand_embed

    return await run_brand_embed(ctx)
