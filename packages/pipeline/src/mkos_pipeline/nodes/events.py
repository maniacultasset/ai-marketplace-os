from collections.abc import Awaitable, Callable
from typing import Any

from mkos_schemas.enums import NodeStatus, PipelineStage

from mkos_pipeline.state import GraphState


async def emit(
    state: GraphState,
    node: PipelineStage,
    status: NodeStatus,
    detail: dict[str, Any] | None = None,
) -> None:
    await state.deps.emit_node_event(node, status, detail)


async def run_stage(
    state: GraphState,
    node: PipelineStage,
    body: Callable[[GraphState], Awaitable[None]],
    *,
    detail_ok: dict[str, Any] | None = None,
) -> None:
    await emit(state, node, "started")
    try:
        await body(state)
    except Exception:
        await emit(state, node, "failed")
        raise
    await emit(state, node, "ok", detail_ok)
