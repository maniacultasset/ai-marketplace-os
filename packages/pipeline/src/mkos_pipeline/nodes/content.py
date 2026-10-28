from typing import Any

from mkos_schemas.agent import AgentBlockStatus
from mkos_schemas.enums import AgentTrigger
from mkos_schemas.pipeline import ModelCredentials

from mkos_pipeline.exceptions import DeadlineExceeded, ModelCallError
from mkos_pipeline.llm.client import complete_json
from mkos_pipeline.llm.safety import SHARED_SYSTEM_PREFIX
from mkos_pipeline.nodes.events import emit
from mkos_pipeline.nodes.retrieve_brand import chunks_for_prompt
from mkos_pipeline.product import (
    banned_terms,
    compact_rules,
    dotted_get,
    identity,
    product_attributes,
)
from mkos_pipeline.prompts import load_prompt, prompt_version
from mkos_pipeline.schemas_local import ContentDraft
from mkos_pipeline.state import GraphState


def _content_data(state: GraphState) -> dict[str, Any]:
    previous = None
    if state.ctx.previous_version is not None:
        previous = state.ctx.previous_version.content.model_dump()
    return {
        "identity": identity(state.ctx.product),
        "attributes": product_attributes(state.ctx.product),
        "family": state.ctx.product.get("family"),
        "brand_chunks": {
            facet: chunks_for_prompt(state.chunks_by_facet.get(facet, []))
            for facet in ("tone", "glossary", "compliance")
        },
        "user_instructions": state.ctx.user_instructions,
        "review_feedback": state.ctx.review_feedback,
        "previous_version_content": previous,
        "rules": compact_rules(state.ctx.marketplace_rules),
        "banned_terms": list(banned_terms(state.ctx.marketplace_rules)),
    }


def _keep_backed_claims(draft: ContentDraft, product: dict[str, Any]) -> ContentDraft:
    kept = []
    for claim in draft.claims:
        if claim.fact_keys and all(dotted_get(product, key) is not None for key in claim.fact_keys):
            kept.append(claim)
    return draft.model_copy(update={"claims": kept})


async def _generate_content(
    state: GraphState,
    credentials: ModelCredentials,
    *,
    trigger: AgentTrigger,
    prompt_name: str,
    extra_data: dict[str, Any] | None = None,
    attempt: int,
) -> ContentDraft:
    if trigger == "correct":
        data = extra_data or {}
    else:
        data = _content_data(state)
        if extra_data:
            data.update(extra_data)
    draft = await complete_json(
        credentials=credentials,
        purpose="generation",
        prompt_version=prompt_version(prompt_name, "v1"),
        system=SHARED_SYSTEM_PREFIX,
        developer=load_prompt(prompt_name, "v1"),
        data=data,
        response_model=ContentDraft,
        deadline_at=state.deadline_at,
        agent_name="content" if trigger == "generate" else "correction",
        trigger=trigger,
        attempt=attempt,
        records=state.records,
        ctx=state.ctx,
    )
    return _keep_backed_claims(draft, state.ctx.product)


async def content_agent(
    state: GraphState, credentials: ModelCredentials, *, announce_start: bool = True
) -> None:
    if announce_start:
        await emit(state, "content_agent", "started")
    try:
        draft = await _generate_content(
            state,
            credentials,
            trigger="generate",
            prompt_name="content",
            attempt=1,
        )
    except (ModelCallError, DeadlineExceeded) as exc:
        state.upsert_block(
            AgentBlockStatus(agent="content", status="failed"),
        )
        await emit(state, "content_agent", "failed", {"error": type(exc).__name__})
        return
    if not draft.title.strip():
        state.upsert_block(AgentBlockStatus(agent="content", status="failed"))
        await emit(state, "content_agent", "failed", {"error": "empty_title"})
        return
    state.content_draft = draft
    run_id = state.records[-1].id if state.records else None
    state.upsert_block(AgentBlockStatus(agent="content", status="ok", agent_run_id=run_id))
    await emit(state, "content_agent", "ok")


async def correct_content(
    state: GraphState,
    credentials: ModelCredentials,
    extra_data: dict[str, Any],
    attempt: int,
) -> None:
    try:
        draft = await _generate_content(
            state,
            credentials,
            trigger="correct",
            prompt_name="content_correct",
            extra_data=extra_data,
            attempt=attempt,
        )
    except (ModelCallError, DeadlineExceeded):
        state.upsert_block(AgentBlockStatus(agent="content", status="failed"))
        return
    if not draft.title.strip():
        state.upsert_block(AgentBlockStatus(agent="content", status="failed"))
        return
    state.content_draft = draft
    run_id = state.records[-1].id if state.records else None
    state.upsert_block(AgentBlockStatus(agent="content", status="ok", agent_run_id=run_id))
