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


# ---- SI10 T11 (AC8): the AI pause and provider refusals ----


class _AlertSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, kind: str, **_: Any) -> bool:
        self.calls.append(kind)
        return True


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> _AlertSpy:
    from app.workers import budget

    spy = _AlertSpy()
    monkeypatch.setattr(budget, "alert", spy)
    return spy


@pytest.fixture
def key_answer(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """`/key` stand-in (no network): set `[0]` to `refused` or `ok`."""
    from app.adapters.openrouter.key import KeyInfo, KeyRefused
    from app.core.config import get_settings
    from app.workers import budget

    answer = ["ok"]

    async def fake_key(api_key: str, **_: Any) -> Any:
        if answer[0] == "refused":
            raise KeyRefused("HTTP 401")
        return KeyInfo(limit=Decimal(10), remaining=Decimal(5), usage=Decimal(5))

    monkeypatch.setattr(budget, "fetch_key", fake_key)
    monkeypatch.setenv("LLM_GATEWAY", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "Zq9openrouter-test")
    get_settings.cache_clear()
    budget.reset_key_confirmation()
    yield answer
    budget.reset_key_confirmation()


def _paused(conn: Conn) -> Any:
    row = conn.execute("SELECT paused_reason FROM ai_credit_state WHERE id = 1").fetchone()
    assert row is not None
    return row[0]


def _wire(status: int) -> Exception:
    from app.services.embeddings import EmbeddingFailed
    from mkos_schemas.codes import ErrorCode

    if status == 402:
        return EmbeddingFailed(ErrorCode.AI_CREDIT_EXHAUSTED, retryable=False, status=402)
    return EmbeddingFailed(
        ErrorCode.MODEL_UNAVAILABLE, retryable=False, status=status, key_suspect=True
    )


async def test_document_embed_refused_while_paused(
    products_world: World, auth_db: Conn, store: MemoryStore, fake: FakeEmbedder
) -> None:
    auth_db.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now()"
    )
    doc, job = await _upload(products_world)
    await _run(job)
    row = job_row(auth_db, job)
    assert (row["status"], row["error_code"]) == ("failed", "AI_CREDIT_EXHAUSTED")
    assert row["cost_reserved_usd"] == 0
    assert doc_row(auth_db, doc)["status"] == "failed"
    assert fake.calls == []


async def test_document_embed_402_pauses_and_releases(
    products_world: World,
    auth_db: Conn,
    store: MemoryStore,
    fake: FakeEmbedder,
    alerts: _AlertSpy,
) -> None:
    fake.fail = {1: _wire(402)}
    _doc, job = await _upload(products_world)
    await _run(job)
    row = job_row(auth_db, job)
    assert (row["status"], row["error_code"]) == ("failed", "AI_CREDIT_EXHAUSTED")
    assert row["cost_reserved_usd"] == 0
    assert _paused(auth_db) == "credit_exhausted"
    assert alerts.calls == ["llm_provider_refused"]


async def test_document_embed_403_not_confirmed_no_pause(
    products_world: World,
    auth_db: Conn,
    store: MemoryStore,
    fake: FakeEmbedder,
    alerts: _AlertSpy,
    key_answer: list[str],
) -> None:
    """Uploaded document text reaches the embedder: a moderation 403 must not pause anyone."""
    fake.fail = {1: _wire(403)}
    _doc, job = await _upload(products_world)
    await _run(job)
    row = job_row(auth_db, job)
    assert (row["status"], row["error_code"]) == ("failed", "MODEL_UNAVAILABLE")
    assert _paused(auth_db) is None
    assert alerts.calls == []


async def test_document_embed_401_confirmed_pauses_key_invalid(
    products_world: World,
    auth_db: Conn,
    store: MemoryStore,
    fake: FakeEmbedder,
    alerts: _AlertSpy,
    key_answer: list[str],
) -> None:
    key_answer[0] = "refused"
    fake.fail = {1: _wire(401)}
    _doc, job = await _upload(products_world)
    await _run(job)
    row = job_row(auth_db, job)
    assert (row["status"], row["error_code"]) == ("failed", "AI_PROVIDER_KEY_INVALID")
    assert _paused(auth_db) == "key_invalid"
    assert alerts.calls == ["llm_provider_refused"]
