import asyncio

from mkos_schemas.pipeline import BrandChunkDTO

from mkos_pipeline.nodes.events import emit
from mkos_pipeline.product import identity
from mkos_pipeline.state import GraphState


def _query(state: GraphState) -> str:
    title = str(identity(state.ctx.product).get("title") or state.ctx.product.get("sku") or state.ctx.category)
    return f"{state.ctx.category} {title}".strip()[:2000]


async def retrieve_brand(state: GraphState) -> None:
    await emit(state, "retrieve_brand", "started")
    query = _query(state)
    tenant = state.ctx.tenant_id
    try:
        tone, glossary, compliance = await asyncio.gather(
            state.deps.retrieve_brand_chunks(tenant, query, k=6, types=["tone_rule", "example"]),
            state.deps.retrieve_brand_chunks(tenant, query, k=4, types=["glossary"]),
            state.deps.retrieve_brand_chunks(tenant, query, k=4, types=["compliance"]),
        )
    except Exception:
        await emit(state, "retrieve_brand", "failed")
        raise
    state.chunks_by_facet = {
        "tone": list(tone),
        "glossary": list(glossary),
        "compliance": list(compliance),
    }
    await emit(
        state,
        "retrieve_brand",
        "ok",
        {facet: len(chunks) for facet, chunks in state.chunks_by_facet.items()},
    )


def chunks_for_prompt(chunks: list[BrandChunkDTO]) -> list[dict[str, str]]:
    return [{"id": str(chunk.id), "type": chunk.type, "text": chunk.text} for chunk in chunks]
