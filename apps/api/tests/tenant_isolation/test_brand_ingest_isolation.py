"""S16 T21 (DW3, AC4, AC9), as `app_rw` on mos_iso: brand documents ingested through the real
`brand_embed` body stay with their tenant. `BrandChunkRepository.search` for acme never returns a
bharat chunk, for any type, and acme's repositories never see bharat's documents or agent runs."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from tests.integration.auth_support import World, build_world
from tests.integration.brand_support import MemoryStore, finish_jobs, principal

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]
TYPES = ("tone_rule", "glossary", "compliance")
PATH = "/api/v1/brand/documents"


@pytest.fixture
def world(clean_tenants: None, admin_conn: Conn) -> World:
    return build_world(admin_conn)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryStore]:
    from app.services import brand_embed
    from app.services.brand_storage import use_document_store
    from app.services.embeddings import HashEmbedder

    memory = MemoryStore()
    use_document_store(memory)
    monkeypatch.setattr(brand_embed, "embedder", lambda: HashEmbedder("text-embedding-3-small"))
    yield memory
    use_document_store(None)


async def _ingest(world: World, slug: str, chunk_type: str, text: str) -> uuid.UUID:
    from app.repositories.uow import tenant_uow
    from app.services.brand_documents import accept_upload
    from app.services.brand_embed import run_brand_embed
    from app.workers.registry import JobContext
    from mkos_schemas.enums import JobKind

    who = principal(world, f"owner@{slug}.test", slug)
    result = await accept_upload(who, uuid.uuid4().hex, PATH, "g.md", text.encode(), chunk_type)  # type: ignore[arg-type]
    assert result.status == 202, result.body
    job_id = uuid.UUID(result.body["job_id"])
    async with tenant_uow(world.tenant(slug)) as uow:
        job = await uow.job_runs.get_by_id(job_id)
        assert job is not None
        payload: dict[str, Any] = dict(job.payload_json or {})
    ctx = JobContext(job_id, world.tenant(slug), JobKind.BRAND_EMBED, "req-iso", payload, 1)
    await run_brand_embed(ctx)
    finish_jobs(world.conn, "succeeded")  # the body ran outside the wrapper: close the job
    return uuid.UUID(payload["document_id"])


async def test_brand_chunk_search_ingested_isolation(world: World, store: MemoryStore) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.embeddings import HashEmbedder

    for chunk_type in TYPES:
        shared = f"Kurta set guidance for {chunk_type} in our brand voice."
        await _ingest(world, "acme", chunk_type, f"{shared}\n\n- acme only {chunk_type}")
        await _ingest(world, "bharat", chunk_type, f"{shared}\n\n- bharat only {chunk_type}")
    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    query = (
        await HashEmbedder("text-embedding-3-small").embed(["kurta set guidance brand voice"])
    ).vectors[0]
    async with tenant_uow(acme) as uow:
        for chunk_type in TYPES:
            found = await uow.brand_chunks.search(query, 50, [chunk_type])  # type: ignore[list-item]
            assert found and all(c.tenant_id == acme for c in found)
            assert not any("bharat" in c.text for c in found)
        everything = await uow.brand_chunks.search(query, 50)
    assert len(everything) == 6 and {c.tenant_id for c in everything} == {acme}
    async with tenant_uow(bharat) as uow:
        theirs = await uow.brand_chunks.search(query, 50)
    assert len(theirs) == 6 and {c.tenant_id for c in theirs} == {bharat}


async def test_brand_documents_other_tenant_invisible(world: World, store: MemoryStore) -> None:
    from app.repositories.uow import tenant_uow

    theirs = await _ingest(world, "bharat", "tone_rule", "Bharat's own voice.")
    async with tenant_uow(world.tenant("acme")) as uow:
        assert await uow.brand_documents.get_live(theirs) is None
        assert (await uow.brand_documents.list_page(limit=50)).items == []
        assert await uow.brand_documents.soft_delete(theirs) is False
        assert (
            await uow.brand_chunks.count_active("brand_document", theirs, "text-embedding-3-small")
            == 0
        )
    async with tenant_uow(world.tenant("bharat")) as uow:
        assert await uow.brand_documents.get_live(theirs) is not None


async def test_agent_runs_other_tenant_invisible(
    world: World, store: MemoryStore, app_conn: Conn
) -> None:
    await _ingest(world, "bharat", "tone_rule", "Bharat's own voice.")
    app_conn.execute("SELECT set_config('app.tenant_id', %s, false)", [str(world.tenant("acme"))])
    assert app_conn.execute("SELECT count(*) FROM agent_runs").fetchone() == (0,)
    app_conn.execute("SELECT set_config('app.tenant_id', %s, false)", [str(world.tenant("bharat"))])
    assert app_conn.execute("SELECT count(*) FROM agent_runs").fetchone() == (1,)
