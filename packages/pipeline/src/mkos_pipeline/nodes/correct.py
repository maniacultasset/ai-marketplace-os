from collections import defaultdict
from typing import Any

from mkos_schemas.errors import ErrorItem
from mkos_schemas.pipeline import ModelCredentials

from mkos_pipeline.llm.deadline import STOP_CORRECTIONS_WHEN, seconds_left
from mkos_pipeline.nodes.attribute import correct_attributes
from mkos_pipeline.nodes.content import correct_content
from mkos_pipeline.nodes.events import emit
from mkos_pipeline.nodes.keyword import correct_keywords
from mkos_pipeline.nodes.merge import merge_outputs
from mkos_pipeline.nodes.validate import validate_rules
from mkos_pipeline.state import GraphState

_FIELD_OWNERS = (
    ("content.", "content"),
    ("keywords.", "keyword"),
    ("attributes", "attribute"),
)


def _owner_for(field: str) -> str | None:
    for prefix, owner in _FIELD_OWNERS:
        if field.startswith(prefix) or field == prefix.rstrip("."):
            return owner
    if field in {"title", "bullets", "description"}:
        return "content"
    if field in {"search_terms", "hidden_terms"}:
        return "keyword"
    return None


def _group_errors(items: list[ErrorItem]) -> dict[str, list[ErrorItem]]:
    grouped: dict[str, list[ErrorItem]] = defaultdict(list)
    for item in items:
        if item.severity != "error":
            continue
        owner = _owner_for(item.field)
        if owner is None:
            continue
        grouped[owner].append(item)
    return grouped


def _previous_value(state: GraphState, field: str) -> Any:
    if state.draft is None:
        return None
    if field.startswith("content."):
        return getattr(state.draft.content, field.split(".", 1)[1], None)
    if field.startswith("keywords."):
        return getattr(state.draft.keywords, field.split(".", 1)[1], None)
    if field.startswith("attributes."):
        return state.draft.attributes.get(field.split(".", 1)[1])
    if field == "attributes":
        return state.draft.attributes
    return None


def remaining_errors(state: GraphState) -> list[ErrorItem]:
    if state.last_report is None:
        return []
    return [item for item in state.last_report.errors if item.severity == "error"]


def _payload(state: GraphState, items: list[ErrorItem]) -> list[dict[str, Any]]:
    return [
        {
            "field": item.field,
            "error_code": item.error_code,
            "message": item.message,
            "suggestion": item.suggestion,
            "previous_value": _previous_value(state, item.field),
        }
        for item in items
    ]


async def correct_loop(state: GraphState, credentials: ModelCredentials) -> None:
    await emit(state, "correct", "started")
    while remaining_errors(state):
        if state.attempts_used >= state.ctx.max_attempts or state.attempts_used >= 3:
            await emit(state, "correct", "ok", {"reason": "attempts_exhausted"})
            return
        if seconds_left(state.deadline_at) < STOP_CORRECTIONS_WHEN.total_seconds():
            await emit(state, "correct", "ok", {"reason": "deadline"})
            return

        grouped = _group_errors(remaining_errors(state))
        if not grouped:
            await emit(state, "correct", "ok", {"reason": "no_owning_agent"})
            return

        attempt = min(state.attempts_used + 1, 3)
        if "content" in grouped:
            await correct_content(state, credentials, {"errors": _payload(state, grouped["content"])}, attempt)
        if "keyword" in grouped:
            await correct_keywords(state, credentials, {"errors": _payload(state, grouped["keyword"])}, attempt)
        if "attribute" in grouped:
            await correct_attributes(state, credentials, {"errors": _payload(state, grouped["attribute"])}, attempt)

        await merge_outputs(state, announce=False)
        if state.draft is None:
            await emit(state, "correct", "failed", {"reason": "merge_failed"})
            return
        await validate_rules(state, announce=False)

    await emit(state, "correct", "ok", {"reason": "clean"})
