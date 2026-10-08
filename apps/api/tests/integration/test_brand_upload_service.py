"""S16 T11 (AC1, AC6, AC8a; CP4-CP7, CP10): the upload and delete services, as app_rw, with an
in-memory store."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from .auth_support import World, audit_rows
from .brand_support import GUIDE, Conn, MemoryStore, chunk_rows, doc_row, finish_jobs, principal
from .conftest import LOCAL_HOSTS
from .jobs_support import count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PATH = "/api/v1/brand/documents"


@pytest.fixture
def store() -> Iterator[MemoryStore]:
    from app.services.brand_storage import use_document_store

    memory = MemoryStore()
    use_document_store(memory)
    yield memory
    use_document_store(None)


async def _upload(
    world: World,
    data: bytes = GUIDE,
    *,
    key: str | None = None,
    name: str = "guide.md",
    chunk_type: str = "tone_rule",
    email: str = "owner@acme.test",
    slug: str = "acme",
) -> Any:
    from app.services.brand_documents import accept_upload

    return await accept_upload(
        principal(world, email, slug), key or uuid.uuid4().hex, PATH, name, data, chunk_type  # type: ignore[arg-type]
    )


def _docs(conn: Conn) -> int:
    return count(conn, "SELECT count(*) FROM brand_documents")


def _jobs(conn: Conn) -> int:
    return count(conn, "SELECT count(*) FROM job_runs")


async def test_upload_creates_document_and_job(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    result = await _upload(products_world)
    assert result.status == 202
    job_id = uuid.UUID(result.body["job_id"])
    assert result.body["poll_url"] == f"/api/v1/jobs/{job_id}"
    doc_id, job, key = auth_db.execute("SELECT id, job_id, storage_key FROM brand_documents").fetchone()  # type: ignore[misc]
    assert job == job_id
    acme = products_world.tenant("acme")
    assert key == f"{acme}/brand-documents/{doc_id}"
    row = auth_db.execute(
        "SELECT kind, entity_type, entity_id, payload_json, created_by FROM job_runs WHERE id = %s",
        [job_id],
    ).fetchone()
    assert row == (
        "brand_embed",
        "brand_document",
        doc_id,
        {"source": "brand_document", "document_id": str(doc_id), "chunk_type": "tone_rule"},
        products_world.user("owner@acme.test"),
    )
    assert store.objects[("marketplace-csv", key)] == (GUIDE, "text/markdown")


async def test_upload_same_bytes_new_key_replays(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    first = await _upload(products_world)
    second = await _upload(products_world, name="again.txt")
    assert second.status == 202 and second.body["job_id"] == first.body["job_id"]
    assert (_docs(auth_db), _jobs(auth_db), store.puts) == (1, 1, 1)


async def test_upload_failed_copy_requeued_once(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    first = await _upload(products_world)
    auth_db.execute(
        "UPDATE brand_documents SET status = 'failed', error_report_json = '{\"x\": 1}'"
    )
    finish_jobs(auth_db, "failed")
    again = await _upload(products_world)
    assert again.status == 202 and again.body["job_id"] != first.body["job_id"]
    doc = auth_db.execute(
        "SELECT status, job_id, error_report_json FROM brand_documents"
    ).fetchone()
    assert doc == ("uploaded", uuid.UUID(again.body["job_id"]), None)
    assert (_docs(auth_db), _jobs(auth_db), store.puts) == (1, 2, 1)
    assert len(audit_rows(auth_db, "brand_document.uploaded")) == 2


async def test_upload_size_cap_413_nothing_stored(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    from app.services.brand_documents import MAX_DOCUMENT_BYTES, PayloadTooLargeError

    key = "size-key"
    with pytest.raises(PayloadTooLargeError):
        await _upload(products_world, b"a" * (MAX_DOCUMENT_BYTES + 1), key=key)
    assert (_docs(auth_db), _jobs(auth_db), store.puts) == (0, 0, 0)
    assert count(auth_db, "SELECT count(*) FROM idempotency_keys") == 0


INVALID = {
    "ext": (b"plain words", "guide.pdf"),
    "no_ext": (b"plain words", ".md"),
    "empty": (b"", "guide.md"),
    "non_utf8": (b"caf\xe9 au lait", "guide.txt"),
    "nul": (b"a\x00b", "guide.txt"),
    "zero_chunks": (b"# only a heading\n\n---\n", "guide.md"),
    "501_chunks": ("\n".join(f"- unit {i}" for i in range(501)).encode(), "guide.md"),
    "secret_shaped": (b'Our voice is warm.\n\n{"api_key": "x"}', "guide.md"),
    "credential_value": (("Note: Atzr|" + "IwEBIA1b2c3d4e5f6g7h8i9j0 here").encode(), "guide.md"),
}


@pytest.mark.parametrize("case", sorted(INVALID))
async def test_upload_validation_422_stored_under_key(
    products_world: World, auth_db: Conn, store: MemoryStore, case: str
) -> None:
    data, name = INVALID[case]
    key = f"bad-{case}"
    result = await _upload(products_world, data, key=key, name=name)
    assert (result.status, result.body) == (422, {"field": "file", "code": "VALIDATION_FAILED"})
    assert (_docs(auth_db), _jobs(auth_db), store.puts) == (0, 0, 0)
    replay = await _upload(products_world, data, key=key, name=name)
    assert (replay.status, replay.body) == (result.status, result.body)


async def test_upload_secret_shaped_422(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    result = await _upload(products_world, INVALID["secret_shaped"][0])
    assert result.status == 422 and "api_key" not in repr(result.body)


async def test_upload_live_cap(products_world: World, auth_db: Conn, store: MemoryStore) -> None:
    from app.services.brand_documents import BrandLimitError

    await _upload(products_world, b"one")
    await _upload(products_world, b"two")
    with pytest.raises(BrandLimitError):
        await _upload(products_world, b"three", key="cap-key")
    assert (_docs(auth_db), _jobs(auth_db), store.puts) == (2, 2, 2)
    finish_jobs(auth_db, "succeeded")
    assert (await _upload(products_world, b"three", key="cap-key")).status == 202


async def test_upload_cap_5_concurrent_never_exceeds_2(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    results = await asyncio.gather(
        *(_upload(products_world, f"text {i}".encode()) for i in range(5)), return_exceptions=True
    )
    assert sum(1 for r in results if not isinstance(r, BaseException)) == 2
    assert _jobs(auth_db) == 2 and _docs(auth_db) == 2


async def test_upload_cap_ignores_profile_jobs(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    from .jobs_support import insert_job

    acme = products_world.tenant("acme")
    for _ in range(3):
        insert_job(
            auth_db,
            acme,
            kind="brand_embed",
            entity_type="brand_document",
            entity_id=acme,
            payload_json={"source": "brand_profile"},
        )
    assert (await _upload(products_world)).status == 202


async def test_upload_cap_other_tenant_unaffected(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    await _upload(products_world, b"one")
    await _upload(products_world, b"two")
    assert (
        await _upload(products_world, b"three", email="owner@bharat.test", slug="bharat")
    ).status == 202


async def test_upload_tenant_document_cap(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    from app.services.brand_documents import BrandLimitError

    from . import factories as f

    acme, owner = products_world.tenant("acme"), products_world.user("owner@acme.test")
    ids = [
        f.brand_document(
            auth_db,
            acme,
            created_by=owner,
            content_hash=f"h{i}",
            storage_key=f"{acme}/brand-documents/{uuid.uuid4()}",
            content_type="text/plain",
            bytes=1,
        )
        for i in range(50)
    ]
    with pytest.raises(BrandLimitError):
        await _upload(products_world)
    assert store.puts == 0
    auth_db.execute("UPDATE brand_documents SET deleted_at = now() WHERE id = %s", [ids[0]])
    auth_db.execute(
        "INSERT INTO brand_chunks (tenant_id, type, text, embedding, embedding_model, content_hash, source_type, source_id, chunk_index) "
        "SELECT %s, 'tone_rule', 't' || g, (SELECT embedding FROM (SELECT array_fill(0.01::real, ARRAY[1536])::vector AS embedding) v), 'text-embedding-3-small', 'h' || g, 'brand_document', %s, g FROM generate_series(0, 19989) g",
        [acme, ids[1]],
    )
    eleven = "\n".join(f"- rule {i}" for i in range(11)).encode()
    with pytest.raises(BrandLimitError):
        await _upload(products_world, eleven)
    ten = "\n".join(f"- rule {i}" for i in range(10)).encode()
    assert (await _upload(products_world, ten)).status == 202


async def test_upload_cap_concurrent_never_exceeds(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    from . import factories as f

    acme, owner = products_world.tenant("acme"), products_world.user("owner@acme.test")
    for i in range(49):
        f.brand_document(
            auth_db,
            acme,
            created_by=owner,
            content_hash=f"h{i}",
            storage_key=f"{acme}/brand-documents/{uuid.uuid4()}",
            content_type="text/plain",
            bytes=1,
            status="ready",
        )
    results = await asyncio.gather(
        *(_upload(products_world, f"text {i}".encode()) for i in range(5)), return_exceptions=True
    )
    assert sum(1 for r in results if not isinstance(r, BaseException)) == 1
    assert count(auth_db, "SELECT count(*) FROM brand_documents WHERE deleted_at IS NULL") == 50


async def test_upload_writes_audit_row(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    from app.core.request_context import request_context

    with request_context("req-upload-1"):
        await _upload(products_world)
    rows = auth_db.execute(
        "SELECT entity_type, entity_id, before, after, request_id, actor_user_id FROM audit_log WHERE action = 'brand_document.uploaded'"
    ).fetchall()
    (doc_id,) = auth_db.execute("SELECT id FROM brand_documents").fetchone()  # type: ignore[misc]
    assert len(rows) == 1
    entity_type, entity_id, before, after, request_id, actor = rows[0]
    assert (entity_type, entity_id, request_id, actor) == (
        "brand_document",
        doc_id,
        "req-upload-1",
        products_world.user("owner@acme.test"),
    )
    assert before in (None, {}) and after in (None, {})


async def test_upload_audit_failure_rolls_back(
    products_world: World, auth_db: Conn, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.identity import AuditRepository

    async def boom(self: Any, **_: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr(AuditRepository, "write", boom)
    with pytest.raises(RuntimeError):
        await _upload(products_world, key="audit-key")
    assert (_docs(auth_db), _jobs(auth_db)) == (0, 0)
    assert count(auth_db, "SELECT count(*) FROM idempotency_keys") == 0


async def test_upload_validation_runs_off_event_loop(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    import time

    body = ("- " * (5 << 20)).encode()  # 10 MiB of bullet markers (T3's worst case)
    ticks: list[float] = []

    async def heartbeat() -> None:
        for _ in range(20):
            start = time.perf_counter()
            await asyncio.sleep(0.01)
            ticks.append(time.perf_counter() - start)

    await asyncio.gather(_upload(products_world, body[:10_000_000]), heartbeat())
    assert max(ticks) < 0.2


async def test_upload_concurrent_identical_one_document(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    first, second = await asyncio.gather(_upload(products_world), _upload(products_world))
    assert first.body["job_id"] == second.body["job_id"]
    assert _docs(auth_db) == 1 and _jobs(auth_db) == 1


async def test_upload_no_transaction_open_during_put(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    seen: list[int] = []
    real = store.put

    async def put(bucket: str, key: str, data: bytes, content_type: str) -> None:
        row = auth_db.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_rw' "
            "AND datname = current_database() AND state LIKE 'idle in transaction%'"
        ).fetchone()
        seen.append(int(row[0]) if row else -1)
        await real(bucket, key, data, content_type)

    store.put = put  # type: ignore[method-assign]
    await _upload(products_world)
    assert seen == [0]


async def test_upload_request_hash_covers_type(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    from app.core.errors import ConflictError

    await _upload(products_world, key="same-key")
    with pytest.raises(ConflictError):
        await _upload(products_world, key="same-key", chunk_type="glossary")
    with pytest.raises(ConflictError):
        await _upload(products_world, b"other bytes", key="same-key")


async def test_upload_filename_path_stripped(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    await _upload(products_world, name="../../etc/‮voice​.md")
    (name,) = auth_db.execute("SELECT filename FROM brand_documents").fetchone()  # type: ignore[misc]
    assert name == "voice.md"


async def test_upload_declared_content_type_ignored(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    await _upload(products_world, name="notes.TXT")
    (ctype,) = auth_db.execute("SELECT content_type FROM brand_documents").fetchone()  # type: ignore[misc]
    assert ctype == "text/plain"


async def test_upload_no_body_in_logs(
    products_world: World, auth_db: Conn, store: MemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    canary = "canary-body-text-3c9a"
    await _upload(products_world, f"Voice: {canary}.".encode(), name="canary-name.md")
    await _upload(products_world, f'{{"api_key": "{canary}"}}'.encode())
    assert canary not in caplog.text and "canary-name" not in caplog.text


async def test_delete_soft_deletes_and_deactivates(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    from app.services.brand_documents import delete_document

    from . import factories as f

    await _upload(products_world)
    doc_id, key = auth_db.execute("SELECT id, storage_key FROM brand_documents").fetchone()  # type: ignore[misc]
    acme = products_world.tenant("acme")
    f.brand_chunk(
        auth_db,
        acme,
        source_type="brand_document",
        source_id=doc_id,
        text="t",
        embedding=f.embedding(),
    )
    await delete_document(principal(products_world), doc_id)
    assert doc_row(auth_db, doc_id)["deleted_at"] is not None
    assert chunk_rows(auth_db, acme) == []
    assert ("marketplace-csv", key) not in store.objects
    assert len(audit_rows(auth_db, "brand_document.deleted")) == 1


async def test_delete_missing_not_found(
    products_world: World, auth_db: Conn, store: MemoryStore
) -> None:
    from app.core.errors import NotFoundError
    from app.services.brand_documents import delete_document

    with pytest.raises(NotFoundError):
        await delete_document(principal(products_world), uuid.uuid4())
    assert audit_rows(auth_db, "brand_document.deleted") == []


async def test_delete_object_failure_logged_not_raised(
    products_world: World, auth_db: Conn, store: MemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    from app.services.brand_documents import delete_document

    await _upload(products_world)
    (doc_id,) = auth_db.execute("SELECT id FROM brand_documents").fetchone()  # type: ignore[misc]
    store.fail_delete = True
    await delete_document(principal(products_world), doc_id)
    assert doc_row(auth_db, doc_id)["deleted_at"] is not None


async def test_delete_audit_failure_rolls_back(
    products_world: World, auth_db: Conn, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.identity import AuditRepository
    from app.services.brand_documents import delete_document

    from . import factories as f

    await _upload(products_world)
    (doc_id,) = auth_db.execute("SELECT id FROM brand_documents").fetchone()  # type: ignore[misc]
    acme = products_world.tenant("acme")
    f.brand_chunk(
        auth_db,
        acme,
        source_type="brand_document",
        source_id=doc_id,
        text="t",
        embedding=f.embedding(),
    )

    async def boom(self: Any, **_: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr(AuditRepository, "write", boom)
    with pytest.raises(RuntimeError):
        await delete_document(principal(products_world), doc_id)
    assert doc_row(auth_db, doc_id)["deleted_at"] is None
    assert len(chunk_rows(auth_db, acme)) == 1
