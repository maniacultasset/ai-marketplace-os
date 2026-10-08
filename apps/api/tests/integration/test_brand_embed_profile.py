"""S16 T13 (AC5; CP6, CP11): the `brand_embed` profile body."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from .auth_support import World
from .brand_support import Conn, FakeEmbedder, agent_rows, chunk_rows
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
MODEL = "text-embedding-3-small"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    from app.services import brand_embed

    embedder = FakeEmbedder()
    monkeypatch.setattr(brand_embed, "embedder", lambda: embedder)
    return embedder


def _voice(
    conn: Conn,
    tenant: uuid.UUID,
    *,
    tone: str | None = None,
    guidelines: list[str] | None = None,
    glossary: dict[str, str] | None = None,
) -> None:
    conn.execute(
        "UPDATE tenants SET tone = %s, guidelines = %s, glossary = %s WHERE id = %s",
        [
            tone,
            None if guidelines is None else Jsonb(guidelines),
            None if glossary is None else Jsonb(glossary),
            tenant,
        ],
    )


async def _run(tenant: uuid.UUID) -> Any:
    from app.services.brand_embed import run_brand_embed
    from app.workers.registry import JobContext
    from mkos_schemas.enums import JobKind

    ctx = JobContext(
        uuid.uuid4(), tenant, JobKind.BRAND_EMBED, "req-profile", {"source": "brand_profile"}, 1
    )
    return await run_brand_embed(ctx)


async def test_profile_chunks_types_and_text(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    acme = products_world.tenant("acme")
    _voice(
        auth_db,
        acme,
        tone="Warm.",
        guidelines=["Never claim best.", "Name the fabric."],
        glossary={"kurta outfit": "kurta set"},
    )
    await _run(acme)
    rows = chunk_rows(auth_db, acme)
    assert [(r["type"], r["text"], r["chunk_index"]) for r in rows] == [
        ("tone_rule", "Never claim best.", 0),
        ("tone_rule", "Name the fabric.", 1),
        ("glossary", "Say 'kurta set' not 'kurta outfit'.", 2),
        ("tone_rule", "Warm.", 3),
    ]
    assert {(r["source_type"], r["source_id"], r["embedding_model"]) for r in rows} == {
        ("brand_profile", acme, MODEL)
    }


async def test_profile_reembed_incremental(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    acme = products_world.tenant("acme")
    _voice(auth_db, acme, guidelines=["One.", "Two.", "Three."])
    await _run(acme)
    _voice(auth_db, acme, guidelines=["One.", "Deux.", "Three."])
    await _run(acme)
    assert fake.calls[-1] == ["Deux."]
    assert [r["text"] for r in chunk_rows(auth_db, acme)] == ["One.", "Deux.", "Three."]
    assert "Two." in [r["text"] for r in chunk_rows(auth_db, acme, active=False)]


async def test_profile_rerun_no_calls(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    acme = products_world.tenant("acme")
    _voice(auth_db, acme, guidelines=["One."])
    await _run(acme)
    outcome = await _run(acme)
    assert outcome.result["unchanged"] is True and len(fake.calls) == 1
    assert len(agent_rows(auth_db, acme)) == 1


async def test_profile_jobs_serialised(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    acme = products_world.tenant("acme")
    _voice(auth_db, acme, guidelines=["One.", "Two."], glossary={"a": "b"})
    results = await asyncio.gather(_run(acme), _run(acme), _run(acme), return_exceptions=True)
    assert not [r for r in results if isinstance(r, BaseException)]
    assert [r["text"] for r in chunk_rows(auth_db, acme)] == ["One.", "Two.", "Say 'b' not 'a'."]


async def test_profile_superseded_rechunks_from_new_values(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    acme = products_world.tenant("acme")
    _voice(auth_db, acme, guidelines=["Old."])
    real = fake.embed

    async def embed_then_change(texts: Any) -> Any:
        batch = await real(texts)
        if len(fake.calls) == 1:
            _voice(auth_db, acme, guidelines=["New."])
        return batch

    fake.embed = embed_then_change  # type: ignore[method-assign]
    await _run(acme)
    assert [r["text"] for r in chunk_rows(auth_db, acme)] == ["New."]


async def test_profile_superseded_three_times_retryable(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    from app.workers.errors import RetryableJobError

    acme = products_world.tenant("acme")
    _voice(auth_db, acme, guidelines=["v0."])
    real = fake.embed

    async def embed_then_change(texts: Any) -> Any:
        batch = await real(texts)
        _voice(auth_db, acme, guidelines=[f"v{len(fake.calls)}."])
        return batch

    fake.embed = embed_then_change  # type: ignore[method-assign]
    with pytest.raises(RetryableJobError):
        await _run(acme)
    assert chunk_rows(auth_db, acme) == []


async def test_profile_cleared_deactivates_all(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    acme = products_world.tenant("acme")
    _voice(auth_db, acme, guidelines=["One."], glossary={"a": "b"})
    await _run(acme)
    _voice(auth_db, acme)
    await _run(acme)
    assert chunk_rows(auth_db, acme) == []


async def test_profile_search_finds_glossary(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.embeddings import HashEmbedder

    acme = products_world.tenant("acme")
    _voice(auth_db, acme, guidelines=["Name the fabric."], glossary={"kurta outfit": "kurta set"})
    await _run(acme)
    query = (await HashEmbedder(MODEL).embed(["kurta set"])).vectors[0]
    async with tenant_uow(acme) as uow:
        found = await uow.brand_chunks.search(query, 5, ["glossary"])
    assert [c.text for c in found] == ["Say 'kurta set' not 'kurta outfit'."]


async def test_profile_patch_on_seeded_tenant_leaves_one_active_set(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    from . import factories as f

    acme = products_world.tenant("acme")
    seeded = uuid.uuid4()  # the S9 seed's uuid5(slug) source id, not the tenant id
    for i, kind in enumerate(("tone_rule", "glossary")):
        f.brand_chunk(
            auth_db,
            acme,
            source_type="brand_profile",
            source_id=seeded,
            chunk_index=i,
            type=kind,
            text=f"seed {i}",
            embedding=f.embedding(),
            embedding_model=MODEL,
        )
    _voice(auth_db, acme, guidelines=["Own."])
    await _run(acme)
    rows = chunk_rows(auth_db, acme)
    assert [(r["source_id"], r["text"]) for r in rows] == [(acme, "Own.")]
    assert {r["source_id"] for r in chunk_rows(auth_db, acme, active=False)} == {seeded}


async def test_profile_body_rechecks_chunks(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    from app.workers.errors import JobFailed

    acme = products_world.tenant("acme")
    _voice(auth_db, acme, guidelines=["Good."])
    await _run(acme)
    _voice(auth_db, acme, guidelines=["Token: Atzr|" + "IwEBIA1b2c3d4e5f6g7h8i9j0"])
    with pytest.raises(JobFailed) as info:
        await _run(acme)
    assert (
        info.value.report.error_code is not None
        and info.value.report.error_code.value == "VALIDATION_FAILED"
    )
    assert len(fake.calls) == 1 and len(agent_rows(auth_db, acme)) == 1
    assert [r["text"] for r in chunk_rows(auth_db, acme)] == ["Good."]
