"""SI10 T10 (AC8-AC10; D5, D6, CP-D15): a provider account refusal fails the generation job with
the true code, pauses AI for everyone only when it is proven, and every terminal path releases
the budget hold. `/key` is a fake (no network, no key)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating
from .jobs_support import job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


class AlertSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, **kw: Any) -> bool:
        self.calls.append((kind, kw))
        return True


class FakeKey:
    """`/key` stand-in: `refused` → `KeyRefused`, `ok` → a 200, `down` → `KeyUnavailable`."""

    def __init__(self) -> None:
        self.answer = "ok"
        self.calls = 0

    async def __call__(self, api_key: str, **_: Any) -> Any:
        from app.adapters.openrouter.key import KeyInfo, KeyRefused, KeyUnavailable

        self.calls += 1
        if self.answer == "refused":
            raise KeyRefused("HTTP 401")
        if self.answer == "down":
            raise KeyUnavailable("ReadTimeout")
        return KeyInfo(limit=Decimal(10), remaining=Decimal(5), usage=Decimal(5))


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> AlertSpy:
    from app.workers import budget

    spy = AlertSpy()
    monkeypatch.setattr(budget, "alert", spy)
    return spy


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeKey]:
    from app.core.config import get_settings
    from app.services import pipeline_deps
    from app.services.embeddings import HashEmbedder
    from app.workers import budget

    fake = FakeKey()
    monkeypatch.setattr(budget, "fetch_key", fake)
    # the gateway switch must not build the real OpenRouter embedder (no network in tests)
    monkeypatch.setattr(pipeline_deps, "get_embedder", lambda s: HashEmbedder(s.embedding_model))
    monkeypatch.setenv("LLM_GATEWAY", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "Zq9openrouter-test")
    get_settings.cache_clear()
    budget.reset_key_confirmation()
    yield fake
    budget.reset_key_confirmation()


class RefusingEmbedder:
    model = "text-embedding-3-small"

    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.calls = 0

    async def embed(self, texts: Any) -> Any:
        self.calls += 1
        raise self.failure


def _embedder(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> RefusingEmbedder:
    from app.services import pipeline_deps

    embedder = RefusingEmbedder(failure)
    monkeypatch.setattr(pipeline_deps, "get_embedder", lambda _settings: embedder)
    return embedder


def _chunks(world: GenWorld, slug: str = "acme") -> None:
    from app.core.config import get_settings

    f.brand_chunk(world.conn, world.tenant(slug), embedding_model=get_settings().embedding_model)


async def _run(job_id: uuid.UUID) -> None:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    register_bodies()
    await run_job(str(job_id))


def _credit(world: GenWorld) -> tuple[Any, ...]:
    row = world.conn.execute(
        "SELECT paused_reason, paused_at FROM ai_credit_state WHERE id = 1"
    ).fetchone()
    assert row is not None
    return tuple(row)


async def _generate(world: GenWorld, scenario: str | None = None) -> Any:
    accepted = await make_generating(
        world, instructions=None if scenario is None else f"stub:{scenario}"
    )
    await _run(accepted.job_id)
    return accepted, job_row(world.conn, accepted.job_id)


def _error(job: dict[str, Any]) -> tuple[str | None, bool | None]:
    report = job["error_report_json"] or {}
    return job["error_code"], report.get("retryable")


# ---- credit (402) ----


@pytest.mark.parametrize("scenario", ["credit", "credit_legacy"])
async def test_stub_credit_refusal_pauses_and_fails_truthfully(
    gen_world: GenWorld, alerts: AlertSpy, scenario: str
) -> None:
    accepted, job = await _generate(gen_world, scenario)
    assert (job["status"], job["error_code"]) == ("failed", "AI_CREDIT_EXHAUSTED")
    assert job["cost_reserved_usd"] == Decimal("0")
    listing = gen_world.listing(accepted.listing_id)
    assert (listing["status"], listing["status_reason"]) == ("failed", "generation_failed")
    assert _credit(gen_world)[0] == "credit_exhausted"
    assert [kind for kind, _ in alerts.calls] == ["llm_provider_refused"]
    assert alerts.calls[0][1]["error_code"] == "AI_CREDIT_EXHAUSTED"
    # invariant 7: the pipeline's failed record is stored
    assert (
        gen_world.count("agent_runs", "job_id = %s AND status = 'failed'", [accepted.job_id]) == 1
    )


async def test_second_refusal_keeps_first_pause_and_alerts_once(
    gen_world: GenWorld, alerts: AlertSpy
) -> None:
    await _generate(gen_world, "credit")
    first = _credit(gen_world)
    accepted = await make_generating(gen_world, sku="KURTA-001", instructions="stub:credit")
    await _run(accepted.job_id)
    job = job_row(gen_world.conn, accepted.job_id)
    assert job["error_code"] == "AI_CREDIT_EXHAUSTED"
    assert _credit(gen_world) == first
    assert len(alerts.calls) == 1


async def test_job_queued_before_pause_refused_at_claim_no_model_call(
    gen_world: GenWorld, alerts: AlertSpy
) -> None:
    accepted = await make_generating(gen_world)
    gen_world.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now()"
    )
    await _run(accepted.job_id)
    job = job_row(gen_world.conn, accepted.job_id)
    assert (job["status"], job["error_code"]) == ("failed", "AI_CREDIT_EXHAUSTED")
    assert job["cost_reserved_usd"] == Decimal("0")
    assert gen_world.count("agent_runs", "job_id = %s", [accepted.job_id]) == 0
    assert alerts.calls == []  # the pause was already set


async def test_key_invalid_pause_refuses_at_claim_with_key_code(
    gen_world: GenWorld, alerts: AlertSpy, key: FakeKey
) -> None:
    accepted = await make_generating(gen_world)
    gen_world.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'key_invalid', paused_at = now()"
    )
    await _run(accepted.job_id)
    assert job_row(gen_world.conn, accepted.job_id)["error_code"] == "AI_PROVIDER_KEY_INVALID"
    assert key.calls == 0  # already confirmed


async def test_query_embed_402_pauses_and_fails_truthfully(
    gen_world: GenWorld, alerts: AlertSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.embeddings import EmbeddingFailed
    from mkos_schemas.codes import ErrorCode

    _chunks(gen_world)
    embedder = _embedder(
        monkeypatch, EmbeddingFailed(ErrorCode.AI_CREDIT_EXHAUSTED, retryable=False, status=402)
    )
    _accepted, job = await _generate(gen_world)
    assert job["error_code"] == "AI_CREDIT_EXHAUSTED"
    assert job["cost_reserved_usd"] == Decimal("0")
    assert embedder.calls == 1
    assert _credit(gen_world)[0] == "credit_exhausted"
    assert len(alerts.calls) == 1


async def test_pause_write_failing_still_fails_with_credit_code(
    gen_world: GenWorld, alerts: AlertSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.ai_credit import AiCreditRepository

    async def boom(self: Any, reason: str, at: Any) -> bool:
        raise RuntimeError("db down")

    monkeypatch.setattr(AiCreditRepository, "pause", boom)
    _accepted, job = await _generate(gen_world, "credit")
    assert job["error_code"] == "AI_CREDIT_EXHAUSTED"
    assert _credit(gen_world) == (None, None)
    assert alerts.calls == []


# ---- key suspicion (401/403), confirmed with /key ----


async def test_wire_401_confirmed_by_key_pauses_key_invalid(
    gen_world: GenWorld, alerts: AlertSpy, key: FakeKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.embeddings import EmbeddingFailed
    from mkos_schemas.codes import ErrorCode

    key.answer = "refused"
    _chunks(gen_world)
    _embedder(
        monkeypatch,
        EmbeddingFailed(ErrorCode.MODEL_UNAVAILABLE, retryable=False, status=401, key_suspect=True),
    )
    _accepted, job = await _generate(gen_world)
    assert job["error_code"] == "AI_PROVIDER_KEY_INVALID"
    assert _credit(gen_world)[0] == "key_invalid"
    assert key.calls == 1
    assert len(alerts.calls) == 1


@pytest.mark.parametrize("answer", ["ok", "down"])
async def test_wire_403_not_confirmed_fails_this_job_only(
    gen_world: GenWorld,
    alerts: AlertSpy,
    key: FakeKey,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    """A moderation 403 on tenant text must never pause AI for every tenant."""
    from datetime import UTC, datetime

    from app.services.embeddings import EmbeddingFailed
    from app.workers.budget import admission
    from mkos_schemas.codes import ErrorCode

    key.answer = answer
    _chunks(gen_world)
    _embedder(
        monkeypatch,
        EmbeddingFailed(ErrorCode.MODEL_UNAVAILABLE, retryable=False, status=403, key_suspect=True),
    )
    _accepted, job = await _generate(gen_world)
    assert _error(job) == ("MODEL_UNAVAILABLE", False)
    assert job["cost_reserved_usd"] == Decimal("0")
    assert _credit(gen_world) == (None, None)
    assert alerts.calls == []
    assert key.calls == 1
    assert await admission(gen_world.tenant("bharat"), datetime.now(UTC)) is None


async def test_error_body_403_makes_no_key_call_and_no_pause(
    gen_world: GenWorld, alerts: AlertSpy, key: FakeKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.embeddings import EmbeddingFailed
    from mkos_schemas.codes import ErrorCode

    _chunks(gen_world)
    _embedder(
        monkeypatch, EmbeddingFailed(ErrorCode.MODEL_UNAVAILABLE, retryable=False, status=403)
    )
    _accepted, job = await _generate(gen_world)
    assert job["error_code"] == "INTERNAL_ERROR"  # a refused embeddings call, as before SI10
    assert key.calls == 0
    assert _credit(gen_world) == (None, None)
    assert alerts.calls == []


@pytest.mark.parametrize(
    ("answer", "code", "paused"),
    [("ok", "MODEL_UNAVAILABLE", None), ("refused", "AI_PROVIDER_KEY_INVALID", "key_invalid")],
)
async def test_stub_key_refused_record_is_confirmed(
    gen_world: GenWorld,
    alerts: AlertSpy,
    key: FakeKey,
    answer: str,
    code: str,
    paused: str | None,
) -> None:
    key.answer = answer
    _accepted, job = await _generate(gen_world, "key_refused")
    assert job["error_code"] == code
    assert _credit(gen_world)[0] == paused
    assert len(alerts.calls) == (1 if paused else 0)


async def test_stub_failed_is_still_generation_failed(
    gen_world: GenWorld, alerts: AlertSpy
) -> None:
    _accepted, job = await _generate(gen_world, "failed")
    assert job["error_code"] == "GENERATION_FAILED"
    assert _credit(gen_world) == (None, None)
    assert alerts.calls == []


# ---- every terminal path releases the hold (D6, CP-D19) ----


async def test_pipeline_timeout_after_model_stage_releases(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def late_timeout(ctx: Any, deps: Any) -> Any:
        from mkos_schemas.enums import PipelineStage

        await deps.emit_node_event(PipelineStage.CONTENT_AGENT, "started")
        raise TimeoutError("a model call passed its deadline")

    monkeypatch.setattr("app.workers.generation.run_listing_pipeline", late_timeout)
    _accepted, job = await _generate(gen_world)
    assert job["error_code"] == "JOB_TIMEOUT"
    assert job["cost_reserved_usd"] == Decimal("0")


async def test_pipeline_exception_after_model_stage_releases(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(ctx: Any, deps: Any) -> Any:
        from mkos_schemas.enums import PipelineStage

        await deps.emit_node_event(PipelineStage.CONTENT_AGENT, "started")
        raise RuntimeError("the graph broke")

    monkeypatch.setattr("app.workers.generation.run_listing_pipeline", broken)
    _accepted, job = await _generate(gen_world)
    assert job["error_code"] == "INTERNAL_ERROR"
    assert job["cost_reserved_usd"] == Decimal("0")
