"""S16 T12 (DW1, AC1, AC3, AC6): the `brand_embed` document body through the job wrapper, and
its terminal and requeue hooks."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from .auth_support import World
from .brand_support import (
    GUIDE,
    Conn,
    FakeEmbedder,
    MemoryStore,
    agent_rows,
    chunk_rows,
    doc_row,
    failure,
    principal,
)
from .conftest import LOCAL_HOSTS
from .jobs_support import job_row, reset_queue

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PATH = "/api/v1/brand/documents"
MODEL = "text-embedding-3-small"


@pytest.fixture
def store(auth_db: Conn) -> Iterator[MemoryStore]:
    from app.db.queue import get_queue_app
    from app.services.brand_storage import use_document_store

    reset_queue(auth_db)
    get_queue_app.cache_clear()
    memory = MemoryStore()
    use_document_store(memory)
    yield memory
    use_document_store(None)
    get_queue_app.cache_clear()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    from app.services import brand_embed
    from app.workers.tasks import register_bodies

    register_bodies()
    embedder = FakeEmbedder()
    monkeypatch.setattr(brand_embed, "embedder", lambda: embedder)
    return embedder


async def _upload(
    world: World, data: bytes = GUIDE, slug: str = "acme", chunk_type: str = "tone_rule"
) -> tuple[uuid.UUID, uuid.UUID]:
    from app.services.brand_documents import accept_upload

    result = await accept_upload(principal(world, f"owner@{slug}.test", slug), uuid.uuid4().hex, PATH, "guide.md", data, chunk_type)  # type: ignore[arg-type]
    assert result.status == 202, result.body
    job = uuid.UUID(result.body["job_id"])
    row = job_row(world.conn, job)
    return row["entity_id"], job


async def _run(job: uuid.UUID) -> str:
    from app.workers.errors import RetryLater
    from app.workers.wrapper import run_job

    try:
        await run_job(str(job))
    except RetryLater:
        return "retry"
    return "done"


async def test_brand_document_ingest(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    """DW1: an acme Markdown guide → T90 chunks under acme only, each with the model and hash."""
    doc, job = await _upload(products_world)
    assert await _run(job) == "done"
    acme = products_world.tenant("acme")
    rows = chunk_rows(auth_db, acme)
    assert [r["text"] for r in rows] == [
        "We sound warm, plain and factual.",
        "Say kurta set, not kurta outfit.",
        "Name the fabric first.",
        "Never claim a product cures anything.",
    ]
    assert [r["chunk_index"] for r in rows] == [0, 1, 2, 3]
    assert {(r["source_type"], r["source_id"], r["embedding_model"], r["type"]) for r in rows} == {
        ("brand_document", doc, MODEL, "tone_rule")
    }
    assert all(len(r["content_hash"]) == 64 for r in rows)
    assert chunk_rows(auth_db, products_world.tenant("bharat"), active=None) == []
    row = doc_row(auth_db, doc)
    assert (row["status"], row["chunk_count"]) == ("ready", 4)
    assert job_row(auth_db, job)["status"] == "succeeded"
    assert len(agent_rows(auth_db, acme)) == 1


async def test_brand_embed_rerun_idempotent(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    from app.services.brand_embed import run_brand_embed
    from app.workers.registry import JobContext
    from mkos_schemas.enums import JobKind

    _doc, job = await _upload(products_world)
    await _run(job)
    calls = len(fake.calls)
    ctx = JobContext(
        job,
        products_world.tenant("acme"),
        JobKind.BRAND_EMBED,
        "req-x",
        job_row(auth_db, job)["payload_json"],
        1,
    )
    auth_db.execute("UPDATE brand_documents SET status = 'processing'")
    outcome = await run_brand_embed(ctx)
    assert outcome.result == {"chunks": 4, "embedded": 0}
    assert (
        len(fake.calls) == calls
        and len(chunk_rows(auth_db, products_world.tenant("acme"), active=None)) == 4
    )


async def test_brand_embed_resumes_after_partial_batch(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    text = "\n".join(f"- rule number {i}" for i in range(250)).encode()
    doc, job = await _upload(products_world, text)
    fake.fail = {2: failure()}
    assert await _run(job) == "retry"
    acme = products_world.tenant("acme")
    assert len(chunk_rows(auth_db, acme)) == 100
    assert await _run(job) == "done"
    assert [len(c) for c in fake.calls] == [100, 100, 100, 50]
    assert len(chunk_rows(auth_db, acme)) == 250 and doc_row(auth_db, doc)["chunk_count"] == 250


async def test_brand_embed_retryable_then_succeeds(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    doc, job = await _upload(products_world)
    fake.fail = {1: failure()}
    assert await _run(job) == "retry"
    assert job_row(auth_db, job)["status"] == "queued"
    assert await _run(job) == "done"
    assert doc_row(auth_db, doc)["status"] == "ready"
    assert [r["status"] for r in agent_rows(auth_db)] == ["failed", "completed"]


async def test_brand_embed_dead_letter_marks_failed(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    doc, job = await _upload(products_world)
    fake.fail = {1: failure(), 2: failure(), 3: failure()}
    assert [await _run(job) for _ in range(3)] == ["retry", "retry", "done"]
    assert job_row(auth_db, job)["status"] == "dead_letter"
    row = doc_row(auth_db, doc)
    assert row["status"] == "failed" and row["error_report_json"]["error_code"] == "RETRY_EXHAUSTED"
    assert chunk_rows(auth_db, products_world.tenant("acme")) == []
    assert [r["status"] for r in agent_rows(auth_db)] == ["failed"] * 3


async def test_brand_embed_non_retryable_failed_attempt_1(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    doc, job = await _upload(products_world)
    fake.fail = {1: failure(retryable=False)}
    assert await _run(job) == "done"
    jr = job_row(auth_db, job)
    assert (jr["status"], jr["attempt"]) == ("failed", 1)
    row = doc_row(auth_db, doc)
    assert (
        row["status"] == "failed" and row["error_report_json"]["errors"][0]["source"] == "ai_model"
    )


async def test_delete_during_job(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    from app.services.brand_documents import delete_document

    text = "\n".join(f"- rule number {i}" for i in range(150)).encode()
    doc, job = await _upload(products_world, text)
    real = fake.embed

    async def embed_then_delete(texts: Any) -> Any:
        batch = await real(texts)
        if len(fake.calls) == 2:
            await delete_document(principal(products_world), doc)
        return batch

    fake.embed = embed_then_delete  # type: ignore[method-assign]
    assert await _run(job) == "done"
    assert job_row(auth_db, job)["result_json"] == {"skipped": "document_gone"}
    assert chunk_rows(auth_db, products_world.tenant("acme")) == []


async def test_brand_embed_storage_key_prefix_checked(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    doc, job = await _upload(products_world)
    bharat = products_world.tenant("bharat")
    auth_db.execute(
        "UPDATE brand_documents SET storage_key = %s", [f"{bharat}/brand-documents/{doc}"]
    )
    await _run(job)
    assert job_row(auth_db, job)["status"] == "failed" and fake.calls == []
    assert doc_row(auth_db, doc)["status"] == "failed"


async def test_brand_embed_no_transaction_open_during_embed(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    _doc, job = await _upload(products_world)
    seen: list[int] = []
    real = fake.embed

    async def embed(texts: Any) -> Any:
        row = auth_db.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_rw' AND state LIKE 'idle in transaction%'"
        ).fetchone()
        seen.append(int(row[0]) if row else -1)
        return await real(texts)

    fake.embed = embed  # type: ignore[method-assign]
    await _run(job)
    assert seen == [0]


async def test_old_job_hook_fenced(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.brand_embed import on_brand_embed_terminal

    doc, job = await _upload(products_world)
    new_job = uuid.uuid4()
    auth_db.execute("UPDATE brand_documents SET job_id = %s", [new_job])
    async with tenant_uow(products_world.tenant("acme")) as uow:
        old = await uow.job_runs.get_by_id(job)
        assert old is not None
        uow.session.expunge(old)  # a detached copy: the hook must not flush it
        old.status = "failed"
        old.error_report_json = {"status": "failed"}
        await on_brand_embed_terminal(uow.session, old, None)
        await uow.session.rollback()
    row = doc_row(auth_db, doc)
    assert (row["status"], row["job_id"]) == ("uploaded", new_job)


async def test_staff_requeue_points_document_at_new_job(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.job_hooks import run_requeue_hook

    doc, job = await _upload(products_world)
    fake.fail = {1: failure(retryable=False)}
    await _run(job)
    async with tenant_uow(products_world.tenant("acme")) as uow:
        old, new = await uow.job_runs.requeue(job)
        await run_requeue_hook(uow.session, old, new)
    row = doc_row(auth_db, doc)
    assert (row["status"], row["job_id"], row["error_report_json"]) == ("uploaded", new.id, None)
    assert await _run(new.id) == "done"
    assert doc_row(auth_db, doc)["status"] == "ready"


async def test_brand_embed_hook_none_outcome_on_cancel_and_reap(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    from app.services import jobs
    from app.workers.periodic import reap_once

    doc, job = await _upload(products_world)
    await jobs.cancel(principal(products_world), job, "cancel-key", f"/api/v1/jobs/{job}/cancel")
    row = doc_row(auth_db, doc)
    assert (
        row["status"] == "failed"
        and row["error_report_json"]["errors"][0]["message"] == "embedding job cancelled"
    )

    doc2, job2 = await _upload(products_world, b"another guide")
    auth_db.execute(
        "UPDATE job_runs SET status = 'running', attempt = 3, started_at = now(), lease_expires_at = now() - interval '1 minute' WHERE id = %s",
        [job2],
    )
    await reap_once()
    assert job_row(auth_db, job2)["status"] == "dead_letter"
    assert doc_row(auth_db, doc2)["status"] == "failed"
    assert chunk_rows(auth_db, products_world.tenant("acme")) == []


async def test_brand_embed_hook_profile_payload_noop(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.brand_embed import on_brand_embed_terminal

    doc, job = await _upload(products_world)
    async with tenant_uow(products_world.tenant("acme")) as uow:
        found = await uow.job_runs.get_by_id(job)
        assert found is not None
        uow.session.expunge(found)
        found.payload_json = {"source": "brand_profile"}
        found.status = "failed"
        await on_brand_embed_terminal(uow.session, found, None)
        await uow.session.rollback()
    assert doc_row(auth_db, doc)["status"] == "uploaded"
