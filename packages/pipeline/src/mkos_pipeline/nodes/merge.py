from mkos_schemas.listing import CanonicalListing, ListingContent, ListingKeywords

from mkos_pipeline.nodes.events import emit
from mkos_pipeline.nodes.image import image_ids_from_context
from mkos_pipeline.state import GraphState


async def merge_outputs(state: GraphState, *, announce: bool = True) -> None:
    if announce:
        await emit(state, "merge_outputs", "started")
    if state.content_draft is None or not state.content_draft.title.strip():
        state.draft = None
        if announce:
            await emit(state, "merge_outputs", "failed", {"reason": "missing_content"})
        return

    bullets = [bullet.strip() for bullet in state.content_draft.bullets if bullet.strip()][:10]
    if not bullets:
        bullets = [state.content_draft.title.strip()[:1000]]

    description = state.content_draft.description.strip() or state.content_draft.title.strip()
    attributes: dict[str, str] = {}
    if state.attribute_draft is not None:
        for pick in state.attribute_draft.attributes:
            attributes[pick.key] = pick.value

    keywords = ListingKeywords()
    if state.keyword_draft is not None:
        keywords = ListingKeywords(
            search_terms=state.keyword_draft.search_terms,
            hidden_terms=state.keyword_draft.hidden_terms,
        )

    run_id = None
    content_block = state.block_for("content")
    if content_block is not None:
        run_id = content_block.agent_run_id

    state.draft = CanonicalListing(
        product_id=state.ctx.product_id,
        marketplace=state.ctx.marketplace,
        category=state.ctx.category,
        listing_id=state.ctx.listing_id,
        locale=state.ctx.locale,
        content=ListingContent(
            title=state.content_draft.title.strip()[:500],
            bullets=bullets,
            description=description[:5000],
        ),
        keywords=keywords,
        attributes=attributes,
        images=image_ids_from_context(state),
        generated_by_agent_run_id=run_id,
    )
    if announce:
        await emit(state, "merge_outputs", "ok")
