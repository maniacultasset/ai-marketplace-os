"""The stub graph (filled in by T3)."""

from __future__ import annotations

from mkos_schemas.jobs import GenerationContext, GenerationResult
from mkos_schemas.pipeline import PipelineDeps


async def run_listing_pipeline(ctx: GenerationContext, deps: PipelineDeps) -> GenerationResult:
    raise NotImplementedError
