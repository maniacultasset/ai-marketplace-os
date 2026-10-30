import re
from typing import Any

from mkos_schemas.pipeline import ModelCredentials

from mkos_pipeline.exceptions import DeadlineExceeded, ModelCallError
from mkos_pipeline.llm.client import complete_json
from mkos_pipeline.llm.safety import SHARED_SYSTEM_PREFIX
from mkos_pipeline.nodes.events import emit
from mkos_pipeline.product import (
    INVENTED_CLAIM_TERMS,
    banned_terms,
    compact_rules,
    contains_banned,
    identity,
    lookup_fact,
    max_items_for,
    product_attributes,
)
from mkos_pipeline.prompts import load_prompt, prompt_version
from mkos_pipeline.schema_compat import AgentBlockStatus, AgentTrigger
from mkos_pipeline.schemas_local import KeywordDraft
from mkos_pipeline.state import GraphState

_PUNCT = re.compile(r"[^a-z0-9\s]+")
CONTRACT_TERM_LIMIT = 50
CONTRACT_CHAR_LIMIT = 256


def _keyword_data(state: GraphState) -> dict[str, Any]:
    return {
        "identity": identity(state.ctx.product),
        "attributes": product_attributes(state.ctx.product),
        "brand_names": state.ctx.brand_profile.get("brand_names") or [],
        "rules": compact_rules(state.ctx.marketplace_rules),
        "caps": {
            "search_terms": max_items_for(
                state.ctx.marketplace_rules, "keywords.search_terms", CONTRACT_TERM_LIMIT
            ),
            "hidden_terms": max_items_for(
                state.ctx.marketplace_rules, "keywords.hidden_terms", CONTRACT_TERM_LIMIT
            ),
            "term_chars": CONTRACT_CHAR_LIMIT,
        },
        "banned_terms": list(banned_terms(state.ctx.marketplace_rules)),
    }


def _clean_term(term: str) -> str:
    return _PUNCT.sub("", term.casefold()).strip()


def _term_supported(term: str, product: dict[str, Any]) -> bool:
    folded = term.casefold()
    for claim in INVENTED_CLAIM_TERMS:
        if claim in folded and lookup_fact(product, "certifications")[1] is None:
            if claim in {"organic", "gots"} and lookup_fact(product, "fabric_type")[1] is None:
                return False
            if claim in {"organic", "gots"}:
                fabric = lookup_fact(product, "fabric_type")[1]
                if fabric is None or "organic" not in str(fabric).casefold():
                    return False
    return True


def sanitise_keywords(draft: KeywordDraft, state: GraphState) -> KeywordDraft:
    banned = banned_terms(state.ctx.marketplace_rules)
    search_cap = max_items_for(
        state.ctx.marketplace_rules, "keywords.search_terms", CONTRACT_TERM_LIMIT
    )
    hidden_cap = max_items_for(
        state.ctx.marketplace_rules, "keywords.hidden_terms", CONTRACT_TERM_LIMIT
    )
    brand = str(identity(state.ctx.product).get("brand_name") or "").casefold()

    def accept(term: str, seen: set[str]) -> str | None:
        cleaned = _clean_term(term)[:CONTRACT_CHAR_LIMIT]
        if not cleaned or cleaned in seen:
            return None
        if contains_banned(cleaned, banned):
            return None
        if not _term_supported(cleaned, state.ctx.product):
            return None
        seen.add(cleaned)
        return cleaned

    seen: set[str] = set()
    search: list[str] = []
    for term in draft.search_terms:
        kept = accept(term, seen)
        if kept:
            search.append(kept)
        if len(search) >= search_cap:
            break
    hidden: list[str] = []
    brand_repeats = 0
    for term in draft.hidden_terms:
        kept = accept(term, seen)
        if not kept:
            continue
        if brand and brand in kept:
            brand_repeats += 1
            if brand_repeats > 1:
                continue
        hidden.append(kept)
        if len(hidden) >= hidden_cap:
            break
    if (
        len(search) == 1
        and state.content_draft
        and search[0] == _clean_term(state.content_draft.title)
    ):
        search = []
    return KeywordDraft(search_terms=search, hidden_terms=hidden)


async def _generate_keywords(
    state: GraphState,
    credentials: ModelCredentials,
    *,
    trigger: AgentTrigger,
    prompt_name: str,
    extra_data: dict[str, Any] | None = None,
    attempt: int,
) -> KeywordDraft:
    if trigger == "correct":
        data = extra_data or {}
    else:
        data = _keyword_data(state)
        if extra_data:
            data.update(extra_data)
    draft = await complete_json(
        credentials=credentials,
        purpose="generation",
        prompt_version=prompt_version(prompt_name, "v1"),
        system=SHARED_SYSTEM_PREFIX,
        developer=load_prompt(prompt_name, "v1"),
        data=data,
        response_model=KeywordDraft,
        deadline_at=state.deadline_at,
        agent_name="keyword" if trigger == "generate" else "correction",
        trigger=trigger,
        attempt=attempt,
        records=state.records,
        ctx=state.ctx,
    )
    return sanitise_keywords(draft, state)


async def keyword_agent(
    state: GraphState, credentials: ModelCredentials, *, announce_start: bool = True
) -> None:
    if announce_start:
        await emit(state, "keyword_agent", "started")
    try:
        draft = await _generate_keywords(
            state,
            credentials,
            trigger="generate",
            prompt_name="keyword",
            attempt=1,
        )
    except (ModelCallError, DeadlineExceeded) as exc:
        state.upsert_block(AgentBlockStatus(agent="keyword", status="failed"))
        await emit(state, "keyword_agent", "failed", {"error": type(exc).__name__})
        return
    state.keyword_draft = draft
    run_id = state.records[-1].id if state.records else None
    state.upsert_block(AgentBlockStatus(agent="keyword", status="ok", agent_run_id=run_id))
    await emit(state, "keyword_agent", "ok")


async def correct_keywords(
    state: GraphState,
    credentials: ModelCredentials,
    extra_data: dict[str, Any],
    attempt: int,
) -> None:
    try:
        draft = await _generate_keywords(
            state,
            credentials,
            trigger="correct",
            prompt_name="keyword_correct",
            extra_data=extra_data,
            attempt=attempt,
        )
    except (ModelCallError, DeadlineExceeded):
        state.upsert_block(AgentBlockStatus(agent="keyword", status="failed"))
        return
    state.keyword_draft = draft
    run_id = state.records[-1].id if state.records else None
    state.upsert_block(AgentBlockStatus(agent="keyword", status="ok", agent_run_id=run_id))
