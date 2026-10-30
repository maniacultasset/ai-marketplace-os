from typing import Any

from mkos_schemas.pipeline import ModelCredentials

from mkos_pipeline.exceptions import DeadlineExceeded, ModelCallError
from mkos_pipeline.llm.client import complete_json
from mkos_pipeline.llm.safety import SHARED_SYSTEM_PREFIX
from mkos_pipeline.nodes.events import emit
from mkos_pipeline.nodes.fact_backed import canonical_attribute_value
from mkos_pipeline.product import lookup_fact, product_attributes, spec_for
from mkos_pipeline.prompts import load_prompt, prompt_version
from mkos_pipeline.schema_compat import AgentBlockStatus, AgentTrigger, CategoryTemplate
from mkos_pipeline.schemas_local import AttributeDraft, AttributePick
from mkos_pipeline.state import GraphState


def _template_keys(template: CategoryTemplate) -> list[str]:
    keys: list[str] = []
    for group in (
        template.required_attributes,
        template.optional_attributes,
        template.conditional_attributes,
    ):
        for key in group:
            if key not in keys and key not in template.unsupported_attributes:
                keys.append(key)
    for key in template.specs:
        if key not in keys and key not in template.unsupported_attributes:
            keys.append(key)
    return keys


def extracted_candidates(state: GraphState) -> list[AttributePick]:
    picks: list[AttributePick] = []
    seen: set[str] = set()
    for key in _template_keys(state.ctx.category_template):
        path, value = lookup_fact(state.ctx.product, key)
        if path is None or value is None:
            continue
        spec = spec_for(state.ctx.category_template, key)
        kept = canonical_attribute_value(key, value, state.ctx.product, spec)
        if kept is None:
            continue
        picks.append(AttributePick(key=key, value=str(kept), source_fact=path))
        seen.add(key)
    for key, value in product_attributes(state.ctx.product).items():
        if key in seen or key in state.ctx.category_template.unsupported_attributes:
            continue
        if _template_keys(state.ctx.category_template) and key not in _template_keys(
            state.ctx.category_template
        ):
            continue
        spec = spec_for(state.ctx.category_template, key)
        kept = canonical_attribute_value(key, value, state.ctx.product, spec)
        if kept is None:
            continue
        picks.append(AttributePick(key=key, value=str(kept), source_fact=f"attributes.{key}"))
    return picks


def _attribute_data(state: GraphState) -> dict[str, Any]:
    template = state.ctx.category_template
    return {
        "candidates": [pick.model_dump() for pick in extracted_candidates(state)],
        "required_attributes": template.required_attributes,
        "optional_attributes": template.optional_attributes,
        "specs": {key: spec.model_dump() for key, spec in template.specs.items()},
        "product_attributes": product_attributes(state.ctx.product),
    }


def filter_draft(draft: AttributeDraft, state: GraphState) -> AttributeDraft:
    kept: list[AttributePick] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for pick in draft.attributes:
        if pick.key in seen or pick.key in state.ctx.category_template.unsupported_attributes:
            dropped.append(pick.key)
            continue
        spec = spec_for(state.ctx.category_template, pick.key)
        value = canonical_attribute_value(pick.key, pick.value, state.ctx.product, spec)
        if value is None:
            dropped.append(pick.key)
            continue
        kept.append(AttributePick(key=pick.key, value=str(value), source_fact=pick.source_fact))
        seen.add(pick.key)
    state.dropped_attributes = dropped
    return AttributeDraft(attributes=kept)


async def _generate_attributes(
    state: GraphState,
    credentials: ModelCredentials,
    *,
    trigger: AgentTrigger,
    prompt_name: str,
    extra_data: dict[str, Any] | None = None,
    attempt: int,
) -> AttributeDraft:
    if trigger == "correct":
        data = extra_data or {}
    else:
        data = _attribute_data(state)
        if extra_data:
            data.update(extra_data)
    draft = await complete_json(
        credentials=credentials,
        purpose="generation",
        prompt_version=prompt_version(prompt_name, "v1"),
        system=SHARED_SYSTEM_PREFIX,
        developer=load_prompt(prompt_name, "v1"),
        data=data,
        response_model=AttributeDraft,
        deadline_at=state.deadline_at,
        agent_name="attribute" if trigger == "generate" else "correction",
        trigger=trigger,
        attempt=attempt,
        records=state.records,
        ctx=state.ctx,
    )
    return filter_draft(draft, state)


async def attribute_agent(
    state: GraphState, credentials: ModelCredentials, *, announce_start: bool = True
) -> None:
    if announce_start:
        await emit(state, "attribute_agent", "started")
    try:
        draft = await _generate_attributes(
            state,
            credentials,
            trigger="generate",
            prompt_name="attribute",
            attempt=1,
        )
    except (ModelCallError, DeadlineExceeded) as exc:
        # Fall back to deterministic extraction so invented-value tests still hold.
        draft = filter_draft(AttributeDraft(attributes=extracted_candidates(state)), state)
        state.attribute_draft = draft
        state.upsert_block(AgentBlockStatus(agent="attribute", status="failed"))
        await emit(state, "attribute_agent", "failed", {"error": type(exc).__name__})
        return
    state.attribute_draft = draft
    run_id = state.records[-1].id if state.records else None
    state.upsert_block(AgentBlockStatus(agent="attribute", status="ok", agent_run_id=run_id))
    await emit(
        state,
        "attribute_agent",
        "ok",
        {"dropped": state.dropped_attributes} if state.dropped_attributes else None,
    )


async def correct_attributes(
    state: GraphState,
    credentials: ModelCredentials,
    extra_data: dict[str, Any],
    attempt: int,
) -> None:
    try:
        draft = await _generate_attributes(
            state,
            credentials,
            trigger="correct",
            prompt_name="attribute_correct",
            extra_data=extra_data,
            attempt=attempt,
        )
    except (ModelCallError, DeadlineExceeded):
        state.upsert_block(AgentBlockStatus(agent="attribute", status="failed"))
        return
    state.attribute_draft = draft
    run_id = state.records[-1].id if state.records else None
    state.upsert_block(AgentBlockStatus(agent="attribute", status="ok", agent_run_id=run_id))
