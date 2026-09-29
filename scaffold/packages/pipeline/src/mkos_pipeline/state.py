"""AgentState - the LangGraph channel schema.

Graph-INTERNAL. It never crosses the HTTP seam, no FE or BE code imports it,
and it is NOT one of the frozen packages/schemas contracts. The frozen objects
stay GenerationContext (in) and GenerationResult (out); initial_state() and
to_result() below are the only supported way in and out, which is what makes
the seam exactly:

    async def run_listing_pipeline(ctx: GenerationContext,
                                   deps: PipelineDeps) -> GenerationResult

Annotated[..., add] channels are fan-in: the four parallel agent branches append
and LangGraph merges. Everything else is last-write-wins and must be written by
one node only.
"""
from __future__ import annotations

from operator import add
from typing import Annotated, Literal, TypedDict, TypeVar

from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.enums import PipelineStage
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.images import ImageReport
from mkos_schemas.jobs import AgentBlockStatus, GenerationContext, GenerationResult
from mkos_schemas.listing import (AttributeValue, CanonicalListing, ImageRef,
                                 ListingContent, ListingKeywords)

T = TypeVar("T")


def last(_current: T, incoming: T) -> T:
    """Single-writer channel: the later node wins, no accumulation."""
    return incoming


class AgentState(TypedDict, total=False):
    ctx: GenerationContext                                    # read-only, never mutated
    stage: Annotated[PipelineStage, last]
    attempt: Annotated[int, last]                             # capped at ctx.max_attempts
    brand_chunks: Annotated[list[str], last]                  # deps.retrieve_brand_chunks
    content: Annotated[ListingContent | None, last]           # content agent
    keywords: Annotated[ListingKeywords | None, last]         # keyword agent
    attributes: Annotated[dict[str, AttributeValue], last]    # attribute agent
    images: Annotated[list[ImageRef], last]                   # image agent
    listing: Annotated[CanonicalListing | None, last]         # merge_outputs node
    errors: Annotated[list[ErrorItem], add]                   # 3 validators fan in
    image_reports: Annotated[list[ImageReport], add]
    agent_runs: Annotated[list[AgentRunRecord], add]          # drained by to_result()
    blocks: Annotated[list[AgentBlockStatus], add]            # per-agent partial success
    validation: Annotated[object | None, last]                # ValidationSummary
    terminal: Annotated[Literal["generated", "needs_review", "blocked", "failed"] | None, last]
    error: Annotated[ErrorReport | None, last]


def initial_state(ctx: GenerationContext) -> AgentState:
    return AgentState(ctx=ctx, stage=PipelineStage.LOADING_CONTEXT, attempt=0,
                      brand_chunks=[], attributes={}, images=[], errors=[],
                      image_reports=[], agent_runs=[], blocks=[])


def to_result(state: AgentState) -> GenerationResult:
    """The single exit. agent_runs has ONE writer - the backend persists these
    rows from here; no node inserts telemetry itself."""
    ctx = state["ctx"]
    listing = state.get("listing")
    terminal = state.get("terminal") or ("failed" if listing is None else "needs_review")
    return GenerationResult(
        request_id=ctx.request_id, job_id=ctx.job_id, listing=listing,
        image_reports=state.get("image_reports", []),
        validation=state.get("validation"),
        agent_runs=state.get("agent_runs", []),
        blocks=state.get("blocks", []),
        attempts_used=state.get("attempt", 0),
        terminal_state=terminal,
        error=state.get("error"),
    )
