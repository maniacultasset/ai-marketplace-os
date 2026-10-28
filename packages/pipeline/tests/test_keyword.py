from tests.fakes import FakeDeps, make_context

from mkos_pipeline.nodes.keyword import sanitise_keywords
from mkos_pipeline.schemas_local import KeywordDraft
from mkos_pipeline.state import GraphState


def test_banned_term_in_model_json_is_dropped_before_validate_rules() -> None:
    ctx = make_context()
    state = GraphState(ctx=ctx, deps=FakeDeps(ctx), deadline_at=ctx.marketplace_rules.loaded_at)
    draft = KeywordDraft(
        search_terms=["cotton kurta", "cheapest kurta", "sale set"],
        hidden_terms=["discount wear"],
    )
    cleaned = sanitise_keywords(draft, state)
    assert "cheapest kurta" not in cleaned.search_terms
    assert "sale set" not in cleaned.search_terms
    assert cleaned.hidden_terms == []
    assert "cotton kurta" in cleaned.search_terms
