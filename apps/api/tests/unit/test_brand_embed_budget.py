"""Brand embeds pass the same daily caps as generation before any paid call.

Every paid embedding call of a `brand_embed` job (document batches and the profile set) goes
through `brand_embed._embed`, which first reserves the call's upper-bound cost against the
tenant and the platform caps (`app.workers.budget.reserve`, the generation lock order) and
settles the reservation to zero once the call is metered in `agent_runs`. A refusal fails the job
with `BUDGET_EXCEEDED` and makes no call. No database: the budget and metering seams are faked.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from app.services import brand_embed
from app.services.brand_chunking import ChunkDraft
from app.services.embeddings import EmbeddingBatch, EmbeddingFailed
from app.workers import budget
from app.workers.errors import JobFailed, RetryableJobError
from app.workers.registry import JobContext
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind

MODEL = "text-embedding-3-small"
TENANT = uuid.UUID("00000000-0000-4000-8000-00000000a001")
JOB = uuid.UUID("00000000-0000-4000-8000-00000000b001")


class Model:
    """Never called: the metered `embed_texts` seam is faked."""

    model = MODEL

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        raise AssertionError("embed_texts is the only caller")


def _ctx(attempt: int = 2) -> JobContext:
    return JobContext(JOB, TENANT, JobKind.BRAND_EMBED, "req-embed", {}, attempt)


def _drafts(*texts: str) -> list[ChunkDraft]:
    return [
        ChunkDraft(index=i, type="tone_rule", text=t, content_hash=f"{i:064x}")
        for i, t in enumerate(texts)
    ]


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    events: list[tuple[Any, ...]] = []

    async def reserve(
        tenant: uuid.UUID, job: uuid.UUID, attempt: int, *, amount: Decimal, **_: Any
    ) -> bool:
        events.append(("reserve", tenant, job, attempt, amount))
        return True

    async def release(tenant: uuid.UUID, job: uuid.UUID, attempt: int) -> bool:
        events.append(("release", tenant, job, attempt))
        return True

    async def embed_texts(model: Any, texts: Sequence[str], **_: Any) -> list[EmbeddingBatch]:
        events.append(("embed", list(texts)))
        return [EmbeddingBatch([[1.0] * 1536 for _ in texts], len(texts), MODEL)]

    monkeypatch.setattr(budget, "reserve", reserve)
    monkeypatch.setattr(budget, "release", release)
    monkeypatch.setattr(brand_embed, "embed_texts", embed_texts)
    return events


async def test_embed_reserves_before_the_call_and_settles_after(
    seams: list[tuple[Any, ...]],
) -> None:
    vectors = await brand_embed._embed(Model(), _drafts("warm and plain", "हिंदी"), _ctx())
    assert len(vectors) == 2
    assert [e[0] for e in seams] == ["reserve", "embed", "release"]
    _, tenant, job, attempt, amount = seams[0]
    assert (tenant, job, attempt) == (TENANT, JOB, 2)
    assert amount == brand_embed.reservation_usd(MODEL, ["warm and plain", "हिंदी"])
    assert seams[2] == ("release", TENANT, JOB, 2)


def test_reservation_is_an_upper_bound_on_the_tokens() -> None:
    """At most one token per UTF-8 byte, priced at the model's list price, never zero."""
    text = "क" * 2000  # 6000 bytes
    assert brand_embed.reservation_usd(MODEL, [text] * 100) == Decimal("0.012000")
    assert brand_embed.reservation_usd(MODEL, ["a"]) > 0


async def test_embed_over_budget_fails_without_a_call(
    seams: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_: Any, **__: Any) -> bool:
        seams.append(("reserve",))
        raise budget.BudgetRefused("global")

    monkeypatch.setattr(budget, "reserve", refuse)
    with pytest.raises(JobFailed) as caught:
        await brand_embed._embed(Model(), _drafts("x"), _ctx())
    assert caught.value.report.error_code is ErrorCode.BUDGET_EXCEEDED
    assert caught.value.report.retryable is False
    assert [e[0] for e in seams] == ["reserve"]


@pytest.mark.parametrize("retryable", [True, False])
async def test_embed_failure_still_settles_the_reservation(
    seams: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch, retryable: bool
) -> None:
    async def fail(*_: Any, **__: Any) -> Any:
        seams.append(("embed",))
        raise EmbeddingFailed(ErrorCode.MODEL_UNAVAILABLE, retryable=retryable, cause="Scripted")

    monkeypatch.setattr(brand_embed, "embed_texts", fail)
    with pytest.raises(RetryableJobError if retryable else JobFailed):
        await brand_embed._embed(Model(), _drafts("x"), _ctx())
    assert [e[0] for e in seams] == ["reserve", "embed", "release"]


async def test_embed_fenced_out_makes_no_call(
    seams: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale attempt whose row a newer attempt owns reserves nothing, so it must not make the
    paid call either (as generation): it fails INTERNAL_ERROR and the wrapper drops the write."""

    async def fenced(*args: Any, **kwargs: Any) -> bool:
        seams.append(("reserve",))
        return False

    monkeypatch.setattr(budget, "reserve", fenced)
    with pytest.raises(JobFailed) as caught:
        await brand_embed._embed(Model(), _drafts("x"), _ctx())
    assert caught.value.report.error_code is ErrorCode.INTERNAL_ERROR
    assert [e[0] for e in seams] == ["reserve"]
