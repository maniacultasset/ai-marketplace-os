"""S16 T10 (AC1, AC5; DATABASE.md:1062-1067): the `BrandChunkRepository` writes, as app_rw."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import event
from tests.timing import budget

from .auth_support import World
from .brand_support import Conn, chunk_rows
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
MODEL = "text-embedding-3-small"


def _chunks(
    source_id: uuid.UUID,
    texts: list[str],
    *,
    source_type: str = "brand_document",
    model: str = MODEL,
    start: int = 0,
) -> list[Any]:
    from app.core.vectors import hash_vector
    from app.repositories.brand_chunks import NewChunk
    from app.services.brand_chunking import chunk_hash

    return [
        NewChunk(
            type="tone_rule",
            text=text,
            embedding=hash_vector(text),
            embedding_model=model,
            content_hash=chunk_hash(text),
            source_type=source_type,
            source_id=source_id,
            chunk_index=start + i,
        )
        for i, text in enumerate(texts)
    ]


async def _insert(tenant: uuid.UUID, rows: list[Any]) -> None:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(tenant) as uow:
        await uow.brand_chunks.insert_many(rows)


async def test_insert_many_then_search_finds(products_world: World, auth_db: Conn) -> None:
    from app.core.vectors import hash_vector
    from app.repositories.uow import tenant_uow

    acme, doc = products_world.tenant("acme"), uuid.uuid4()
    await _insert(acme, _chunks(doc, ["warm and plain", "never claim best"]))
    async with tenant_uow(acme) as uow:
        found = await uow.brand_chunks.search(hash_vector("warm and plain"), 2)
    assert found[0].text == "warm and plain" and found[0].similarity == pytest.approx(1.0)


async def test_insert_twice_no_duplicate(products_world: World, auth_db: Conn) -> None:
    acme, doc = products_world.tenant("acme"), uuid.uuid4()
    rows = _chunks(doc, ["a", "b"])
    await _insert(acme, rows)
    await _insert(acme, rows)
    assert len(chunk_rows(auth_db, acme)) == 2


async def test_deactivate_then_reinsert_same_index(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme, doc = products_world.tenant("acme"), uuid.uuid4()
    await _insert(acme, _chunks(doc, ["old text"]))
    async with tenant_uow(acme) as uow:
        assert await uow.brand_chunks.deactivate_source("brand_document", doc, indexes=[0]) == 1
        await uow.brand_chunks.insert_many(_chunks(doc, ["new text"]))
        assert await uow.brand_chunks.active_for_source("brand_document", doc, MODEL) == {
            0: _chunks(doc, ["new text"])[0].content_hash
        }
    assert [r["text"] for r in chunk_rows(auth_db, acme, active=False)] == ["old text"]


async def test_vectors_by_hash_model_scoped(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme, doc = products_world.tenant("acme"), uuid.uuid4()
    rows = _chunks(doc, ["a"]) + _chunks(doc, ["a"], model="other-model")
    await _insert(acme, rows)
    async with tenant_uow(acme) as uow:
        found = await uow.brand_chunks.vectors_by_hash(
            "brand_document", doc, [rows[0].content_hash], MODEL
        )
        other = await uow.brand_chunks.vectors_by_hash(
            "brand_document", doc, [rows[0].content_hash], "x"
        )
    assert list(found) == [rows[0].content_hash] and len(found[rows[0].content_hash]) == 1536
    assert other == {}


async def test_count_active(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme, doc = products_world.tenant("acme"), uuid.uuid4()
    await _insert(acme, _chunks(doc, ["a", "b", "c"]))
    async with tenant_uow(acme) as uow:
        assert await uow.brand_chunks.count_active("brand_document", doc, MODEL) == 3
        await uow.brand_chunks.deactivate_source("brand_document", doc)
        assert await uow.brand_chunks.count_active("brand_document", doc, MODEL) == 0


async def test_other_tenant_insert_refused(products_world: World, auth_db: Conn) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.repositories.brand_chunks import BrandChunkRepository
    from app.repositories.uow import tenant_uow

    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    async with tenant_uow(acme) as uow:
        foreign = BrandChunkRepository(uow.session, bharat)
        with pytest.raises(CrossTenantQueryError):
            await foreign.insert_many(_chunks(uuid.uuid4(), ["x"]))
        with pytest.raises(CrossTenantQueryError):
            await foreign.deactivate_profile_all()
    assert chunk_rows(auth_db, bharat, active=None) == []


async def test_bulk_500_rows_one_statement(
    products_world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings
    from app.db.session import dispose_engine, get_engine
    from app.repositories.uow import tenant_uow

    # The HNSW insert of 500 vectors alone costs ~3.3 s of server time here, so a loaded runner
    # crosses the app's 15 s statement_timeout. The timeout is not what this test checks: scale it.
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", str(int(budget(60) * 1000)))
    get_settings.cache_clear()
    await dispose_engine()
    acme, doc = products_world.tenant("acme"), uuid.uuid4()
    rows = _chunks(doc, [f"text {i}" for i in range(500)])
    inserts: list[str] = []

    def spy(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO BRAND_CHUNKS"):
            inserts.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", spy)
    try:
        async with tenant_uow(acme) as uow:
            await uow.brand_chunks.insert_many(rows)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert len(inserts) == 1
    assert len(chunk_rows(auth_db, acme)) == 500


async def test_profile_set_includes_foreign_source_id(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme, seeded = products_world.tenant("acme"), uuid.uuid4()
    await _insert(acme, _chunks(seeded, ["seed rule"], source_type="brand_profile"))
    await _insert(acme, _chunks(acme, ["own rule"], source_type="brand_profile"))
    async with tenant_uow(acme) as uow:
        found = await uow.brand_chunks.active_profile_set(MODEL)
        assert [sid for sid, _, _ in found] == [acme, seeded]
        assert await uow.brand_chunks.deactivate_profile_all() == 2
    assert chunk_rows(auth_db, acme) == []


async def test_count_active_of_type(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme = products_world.tenant("acme")
    await _insert(acme, _chunks(uuid.uuid4(), ["a", "b"]))
    await _insert(acme, _chunks(uuid.uuid4(), ["c"]))
    await _insert(acme, _chunks(acme, ["p"], source_type="brand_profile"))
    async with tenant_uow(acme) as uow:
        assert await uow.brand_chunks.count_active_of_type("brand_document") == 3
