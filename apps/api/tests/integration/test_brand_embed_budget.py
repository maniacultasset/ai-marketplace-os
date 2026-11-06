"""Brand embeds pass the tenant and platform daily AI caps before any paid embedding call, with
a reservation on the attempt settled to zero once the call is metered (the generation lock order,
`app/workers/budget.py`). Over a cap the job fails `BUDGET_EXCEEDED`, the document `failed`, and
the embedder is never called. Without this, re-uploading a large document could push the
platform cap over and refuse every brand's generation for the rest of the UTC day."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from . import factories as f
from .auth_support import World
from .brand_support import GUIDE, Conn, FakeEmbedder, MemoryStore, agent_rows, doc_row, principal
from .conftest import LOCAL_HOSTS
from .jobs_support import job_row, reset_queue

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PATH = "/api/v1/brand/documents"


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


async def _upload(world: World, data: bytes = GUIDE) -> tuple[uuid.UUID, uuid.UUID]:
    from app.services.brand_documents import accept_upload

    result = await accept_upload(
        principal(world), uuid.uuid4().hex, PATH, "guide.md", data, "tone_rule"
    )
    assert result.status == 202, result.body
    job = uuid.UUID(result.body["job_id"])
    return job_row(world.conn, job)["entity_id"], job


async def _run(job: uuid.UUID) -> None:
    from app.workers.wrapper import run_job

    await run_job(str(job))


def _spend(conn: Conn, tenant: uuid.UUID, usd: str) -> None:
    f.agent_run(conn, tenant, cost_usd=Decimal(usd), started_at=datetime.now(UTC))


def _assert_refused(world: World, doc: uuid.UUID, job: uuid.UUID, fake: FakeEmbedder) -> None:
    row = job_row(world.conn, job)
    assert (row["status"], row["error_code"]) == ("failed", "BUDGET_EXCEEDED")
    assert row["cost_reserved_usd"] == 0
    document = doc_row(world.conn, doc)
    assert document["status"] == "failed"
    assert document["error_report_json"]["error_code"] == "BUDGET_EXCEEDED"
    assert fake.calls == []


async def test_document_embed_refused_at_tenant_cap(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    acme = products_world.tenant("acme")
    doc, job = await _upload(products_world)
    _spend(auth_db, acme, "2.500000")  # the 2.50 default cap
    await _run(job)
    _assert_refused(products_world, doc, job, fake)
    assert len(agent_rows(auth_db, acme)) == 1  # only the seeded spend: no embedding row


async def test_document_embed_refused_at_platform_cap(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    bharat = products_world.tenant("bharat")
    auth_db.execute("UPDATE tenants SET llm_daily_budget_usd = 100 WHERE id = %s", [bharat])
    doc, job = await _upload(products_world)
    _spend(auth_db, bharat, "12.000000")  # the 12.00 default platform cap
    await _run(job)
    _assert_refused(products_world, doc, job, fake)


async def test_document_embed_reserves_during_the_call_and_settles(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.budget import tenant_state

    acme = products_world.tenant("acme")
    doc, job = await _upload(products_world)
    seen: list[Decimal] = []
    real = fake.embed

    async def watch(texts: Any) -> Any:
        seen.append(Decimal(job_row(auth_db, job)["cost_reserved_usd"]))
        async with tenant_uow(acme) as uow:
            seen.append((await tenant_state(uow, datetime.now(UTC))).reserved)
        return await real(texts)

    fake.embed = watch  # type: ignore[method-assign]
    await _run(job)
    assert doc_row(auth_db, doc)["status"] == "ready"
    assert seen[0] > 0 and seen[1] == seen[0]
    assert job_row(auth_db, job)["cost_reserved_usd"] == 0
    assert [r["status"] for r in agent_rows(auth_db, acme)] == ["completed"]


async def test_profile_embed_refused_at_tenant_cap(
    products_world: World, auth_db: Conn, fake: FakeEmbedder
) -> None:
    from app.services.brand_embed import run_brand_embed
    from app.workers.errors import JobFailed
    from app.workers.registry import JobContext
    from mkos_schemas.enums import JobKind

    acme = products_world.tenant("acme")
    auth_db.execute("UPDATE tenants SET tone = 'Warm and plain.' WHERE id = %s", [acme])
    _spend(auth_db, acme, "2.500000")
    ctx = JobContext(
        uuid.uuid4(), acme, JobKind.BRAND_EMBED, "req-profile", {"source": "brand_profile"}, 1
    )
    with pytest.raises(JobFailed) as caught:
        await run_brand_embed(ctx)
    assert caught.value.report.error_code is not None
    assert caught.value.report.error_code.value == "BUDGET_EXCEEDED"
    assert fake.calls == []
