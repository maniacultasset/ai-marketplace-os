"""S16 tester round 1: attacks through the real app (HTTP → service → repository → Postgres as
app_rw) and the job wrapper, with the in-memory document store and the hash embedder."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import httpx
import pytest

from .auth_support import PREFIX
from .brand_support import GUIDE, FakeEmbedder, MemoryStore, agent_rows, chunk_rows, doc_row
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, as_member, job_row, jobs_db, reset_queue

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
DOCS = f"{PREFIX}/brand/documents"
ME = f"{PREFIX}/tenants/me"
MODEL = "text-embedding-3-small"
BHARAT_GUIDE = b"Bharat speaks in short, bright lines.\n\n- Always name the weave.\n"


@pytest.fixture
def store(jobs_db: JobWorld) -> Iterator[MemoryStore]:
    from app.db.queue import get_queue_app
    from app.services.brand_storage import use_document_store

    reset_queue(jobs_db.conn)
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
    api: httpx.AsyncClient, jw: JobWorld, email: str, data: bytes = GUIDE
) -> uuid.UUID:
    response = await api.post(
        DOCS,
        files={"file": ("guide.md", data, "text/markdown")},
        headers={**as_member(jw, email), "Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["job_id"])


async def _run(job: uuid.UUID) -> None:
    from app.workers.wrapper import run_job

    await run_job(str(job))


async def _search(tenant: uuid.UUID, text: str) -> list[uuid.UUID]:
    from app.repositories.uow import tenant_uow
    from app.services.embeddings import HashEmbedder

    vector = (await HashEmbedder(MODEL).embed([text])).vectors[0]
    async with tenant_uow(tenant) as uow:
        return [c.tenant_id for c in await uow.brand_chunks.search(vector, 50)]


async def test_http_ingest_then_cross_tenant_delete_and_search(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore, fake: FakeEmbedder
) -> None:
    acme, bharat = jobs_db.tenant("acme"), jobs_db.tenant("bharat")
    acme_job = await _upload(api, jobs_db, "owner@acme.test")
    bharat_job = await _upload(api, jobs_db, "owner@bharat.test", BHARAT_GUIDE)
    await _run(acme_job)
    await _run(bharat_job)
    acme_doc = job_row(jobs_db.conn, acme_job)["entity_id"]
    assert doc_row(jobs_db.conn, acme_doc)["status"] == "ready"

    # bharat names acme's document id: 404, and acme's chunks stay active and searchable
    gone = await api.delete(f"{DOCS}/{acme_doc}", headers=as_member(jobs_db, "owner@bharat.test"))
    assert gone.status_code == 404
    assert doc_row(jobs_db.conn, acme_doc)["deleted_at"] is None
    assert len(chunk_rows(jobs_db.conn, acme)) == 4

    # search with bharat's own words never returns acme rows, and the reverse
    assert set(await _search(bharat, "Say kurta set, not kurta outfit.")) == {bharat}
    assert set(await _search(acme, "Always name the weave.")) == {acme}
    # each tenant's agent_runs hold only its own embedding call
    assert {r["tenant_id"] for r in agent_rows(jobs_db.conn, acme)} == {acme}
    assert len(agent_rows(jobs_db.conn, bharat)) == 1

    # bharat's list shows only bharat's document
    listed = await api.get(DOCS, headers=as_member(jobs_db, "owner@bharat.test"))
    assert [d["id"] for d in listed.json()["data"]] == [
        str(job_row(jobs_db.conn, bharat_job)["entity_id"])
    ]


async def test_duplicate_concurrent_body_runs_one_active_set(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore, fake: FakeEmbedder
) -> None:
    """Duplicate delivery of the same job (a reaped lease still running): the two bodies race and
    still leave exactly one active chunk per index and the right chunk_count."""
    from app.services.brand_embed import run_brand_embed
    from app.workers.registry import JobContext
    from mkos_schemas.enums import JobKind

    job = await _upload(api, jobs_db, "owner@acme.test")
    acme = jobs_db.tenant("acme")
    payload = job_row(jobs_db.conn, job)["payload_json"]
    ctx = JobContext(job, acme, JobKind.BRAND_EMBED, "req-dup", payload, 1)
    results = await asyncio.gather(
        run_brand_embed(ctx), run_brand_embed(ctx), return_exceptions=True
    )
    assert not [r for r in results if isinstance(r, BaseException)], results
    rows = chunk_rows(jobs_db.conn, acme)
    assert sorted(r["chunk_index"] for r in rows) == [0, 1, 2, 3]
    doc = doc_row(jobs_db.conn, payload_document(payload))
    assert (doc["status"], doc["chunk_count"]) == ("ready", 4)


def payload_document(payload: dict[str, object]) -> uuid.UUID:
    return uuid.UUID(str(payload["document_id"]))


async def test_delete_then_reupload_same_bytes_is_a_new_document(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore, fake: FakeEmbedder
) -> None:
    """Content dedupe looks at live documents only: after a delete the same bytes ingest again
    as a new document and the old chunks stay inactive."""
    acme = jobs_db.tenant("acme")
    first = await _upload(api, jobs_db, "owner@acme.test")
    await _run(first)
    old_doc = job_row(jobs_db.conn, first)["entity_id"]
    owner = as_member(jobs_db, "owner@acme.test")
    assert (await api.delete(f"{DOCS}/{old_doc}", headers=owner)).status_code == 204
    assert chunk_rows(jobs_db.conn, acme) == []

    second = await _upload(api, jobs_db, "owner@acme.test")
    assert second != first
    await _run(second)
    new_doc = job_row(jobs_db.conn, second)["entity_id"]
    assert new_doc != old_doc
    active = chunk_rows(jobs_db.conn, acme)
    assert {r["source_id"] for r in active} == {new_doc} and len(active) == 4
    assert (await api.delete(f"{DOCS}/{old_doc}", headers=owner)).status_code == 404


@pytest.mark.parametrize("role", ["operator", "reviewer"])
async def test_lower_roles_change_nothing_anywhere(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore, fake: FakeEmbedder, role: str
) -> None:
    """Role escalation: a non-manager can read the profile and list, never PATCH, upload or delete
    (and a refused PATCH enqueues no job)."""
    who = as_member(jobs_db, f"{role}@acme.test")
    job = await _upload(api, jobs_db, "owner@acme.test")
    doc = job_row(jobs_db.conn, job)["entity_id"]
    got = await api.get(ME, headers=who)
    assert got.status_code == 200
    patched = await api.patch(
        ME, json={"tone": "Loud."}, headers={**who, "If-Match": got.headers["ETag"]}
    )
    assert patched.status_code == 403
    assert (await api.get(DOCS, headers=who)).status_code == 200
    assert (await api.delete(f"{DOCS}/{doc}", headers=who)).status_code == 403
    upload = await api.post(
        DOCS,
        files={"file": ("x.md", b"Another guide.", "text/markdown")},
        headers={**who, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert upload.status_code == 403
    profile_jobs = jobs_db.conn.execute(
        "SELECT count(*) FROM job_runs WHERE payload_json->>'source' = 'brand_profile'"
    ).fetchone()
    assert profile_jobs is not None and profile_jobs[0] == 0
    assert doc_row(jobs_db.conn, doc)["deleted_at"] is None
