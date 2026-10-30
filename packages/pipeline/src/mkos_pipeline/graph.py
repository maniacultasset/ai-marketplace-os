from __future__ import annotations

import asyncio

from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import GenerationContext, GenerationResult
from mkos_schemas.pipeline import PipelineDeps
from mkos_schemas.validation import ValidationSummary

from mkos_pipeline.llm.deadline import Deadline
from mkos_pipeline.nodes.attribute import attribute_agent
from mkos_pipeline.nodes.content import content_agent
from mkos_pipeline.nodes.correct import correct_loop, remaining_errors
from mkos_pipeline.nodes.events import emit
from mkos_pipeline.nodes.image import image_agent
from mkos_pipeline.nodes.keyword import keyword_agent
from mkos_pipeline.nodes.load_context import load_context
from mkos_pipeline.nodes.merge import merge_outputs
from mkos_pipeline.nodes.quality import run_claim_check
from mkos_pipeline.nodes.retrieve_brand import retrieve_brand
from mkos_pipeline.nodes.validate import validate_rules, validate_schema, validate_vision
from mkos_pipeline.schema_compat import AgentBlockStatus, BlockAgent, TerminalState
from mkos_pipeline.state import GraphState

_BLOCK_AGENTS: tuple[BlockAgent, ...] = ("content", "keyword", "attribute", "image")


def _generation_failed(message: str, request_id: str) -> ErrorReport:
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.GENERATION_FAILED,
        request_id=request_id,
        errors=[
            ErrorItem(
                field="listing",
                error_code=ErrorCode.GENERATION_FAILED,
                message=message[:1000],
                retryable=False,
                source="ai_model",
                severity="error",
            )
        ],
    )


def _fill_blocks(state: GraphState) -> None:
    for agent in _BLOCK_AGENTS:
        if state.block_for(agent) is None:
            state.upsert_block(AgentBlockStatus(agent=agent, status="skipped"))


def _map_terminal(state: GraphState) -> TerminalState:
    if state.draft is None:
        return "failed"
    if remaining_errors(state):
        return "blocked"
    failed_block = any(block.status == "failed" for block in state.blocks)
    warnings = False
    if state.last_report is not None:
        warnings = any(item.severity == "warning" for item in state.last_report.errors)
    if failed_block or warnings:
        return "needs_review"
    if state.last_report is None or state.last_report.status != "passed":
        return "needs_review"
    return "generated"


def _build_result(state: GraphState) -> GenerationResult:
    _fill_blocks(state)
    terminal = _map_terminal(state)
    error = None
    if terminal == "failed":
        error = _generation_failed("Generation produced no listing.", state.ctx.request_id)
    elif terminal == "blocked":
        error = state.last_report
    validation = None
    if state.draft is not None and state.last_report is not None:
        errors = state.last_report.errors
        validation = ValidationSummary(
            listing_id=state.ctx.listing_id,
            passed=state.last_report.status == "passed",
            request_id=state.ctx.request_id,
            blocking_count=sum(1 for item in errors if item.severity == "error"),
            warning_count=sum(1 for item in errors if item.severity == "warning"),
        )
    return GenerationResult(
        request_id=state.ctx.request_id,
        job_id=state.ctx.job_id,
        listing=state.draft if terminal != "failed" else None,
        image_reports=state.image_reports,
        validation=validation,
        agent_runs=state.records[:100],
        blocks=state.blocks,
        attempts_used=min(state.attempts_used, 3),
        terminal_state=terminal,
        error=error,
    )


async def _quality_check(state: GraphState) -> None:
    await emit(state, "quality_check", "started")
    if state.draft is not None:
        result = run_claim_check(state.draft, state.ctx.product, state.ctx.marketplace_rules)
        state.draft = result.listing
        await emit(state, "quality_check", "ok", {"claim_check": "skipped"})
        return
    await emit(state, "quality_check", "ok", {"claim_check": "skipped"})


async def run_listing_pipeline(ctx: GenerationContext, deps: PipelineDeps) -> GenerationResult:
    deadline = Deadline.start()
    state = GraphState(ctx=ctx, deps=deps, deadline_at=deadline.deadline_at)
    try:
        await load_context(state)
        if not state.locale_ok:
            return _build_result(state)

        await retrieve_brand(state)

        credentials = await deps.get_model_credentials("generation")
        await emit(state, "content_agent", "started")
        await emit(state, "keyword_agent", "started")
        await emit(state, "attribute_agent", "started")
        await asyncio.gather(
            content_agent(state, credentials, announce_start=False),
            keyword_agent(state, credentials, announce_start=False),
            attribute_agent(state, credentials, announce_start=False),
        )

        await image_agent(state)
        await merge_outputs(state)
        await validate_schema(state)
        await validate_rules(state)
        await validate_vision(state)
        await correct_loop(state, credentials)
        await _quality_check(state)
        return _build_result(state)
    except (asyncio.CancelledError, TimeoutError):
        raise
    except Exception:
        # Port exceptions and programming errors propagate. Model errors are
        # already converted to failed blocks inside the agents.
        raise
