from mkos_schemas.agent import AgentBlockStatus
from mkos_schemas.listing import CanonicalListing, ImageRef, ListingContent

from mkos_pipeline.nodes.events import emit
from mkos_pipeline.state import GraphState


def image_ids_from_context(state: GraphState) -> list[ImageRef]:
    if state.ctx.previous_version is None:
        return []
    return [
        ref
        for ref in state.ctx.previous_version.images
        if ref.slot != "packaging_label"
    ]


async def image_agent(state: GraphState) -> None:
    await emit(state, "image_agent", "started")
    refs = image_ids_from_context(state)
    if not refs:
        state.upsert_block(AgentBlockStatus(agent="image", status="skipped"))
        await emit(state, "image_agent", "ok", {"images": 0})
        return
    listing = CanonicalListing(
        product_id=state.ctx.product_id,
        marketplace=state.ctx.marketplace,
        category=state.ctx.category,
        listing_id=state.ctx.listing_id,
        locale=state.ctx.locale,
        content=ListingContent(title="placeholder", bullets=["placeholder"], description="placeholder"),
        images=refs,
    )
    reports = await state.deps.validate_images(listing)
    state.image_reports = list(reports)
    state.upsert_block(AgentBlockStatus(agent="image", status="ok"))
    await emit(state, "image_agent", "ok", {"images": len(refs)})
