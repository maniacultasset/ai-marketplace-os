from mkos_pipeline.nodes.events import emit
from mkos_pipeline.state import GraphState


async def load_context(state: GraphState) -> None:
    await emit(state, "load_context", "started")
    state.locale_ok = state.ctx.locale == "en-IN"
    if not state.locale_ok:
        await emit(state, "load_context", "failed", {"reason": "unsupported_locale"})
        return
    await emit(
        state,
        "load_context",
        "ok",
        {
            "job_id": str(state.ctx.job_id),
            "marketplace": state.ctx.marketplace,
            "category": state.ctx.category,
        },
    )
