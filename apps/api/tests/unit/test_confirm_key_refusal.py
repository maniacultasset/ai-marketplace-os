"""SI10 T6 (AC9; D5, CP-D15): `confirm_key_refusal` asks OpenRouter's `/key` (a fake transport,
never the network) whether an upstream 401/403 means the key itself is bad. One call per process
per 60 s; concurrent callers share the answer."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.workers import budget

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
BODY = {"data": {"limit": 10, "limit_remaining": 4, "usage": 6}}


class Counting(httpx.AsyncBaseTransport):
    def __init__(self, status: int = 200, *, raise_timeout: bool = False) -> None:
        self.calls = 0
        self.status = status
        self.raise_timeout = raise_timeout

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        await asyncio.sleep(0.01)  # let concurrent callers pile up on the lock
        if self.raise_timeout:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(self.status, json=BODY, request=request)


@pytest.fixture(autouse=True)
def openrouter(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.core.config import get_settings

    monkeypatch.setenv("LLM_GATEWAY", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "Zq9openrouter-test")
    get_settings.cache_clear()
    budget.reset_key_confirmation()
    yield
    budget.reset_key_confirmation()
    get_settings.cache_clear()


@pytest.mark.parametrize(("status", "want"), [(401, True), (403, True), (200, False), (500, False)])
async def test_answer_by_status(status: int, want: bool) -> None:
    transport = Counting(status)
    assert await budget.confirm_key_refusal(now=NOW, transport=transport) is want
    assert transport.calls == 1


async def test_timeout_is_not_a_refusal() -> None:
    transport = Counting(raise_timeout=True)
    assert await budget.confirm_key_refusal(now=NOW, transport=transport) is False
    assert transport.calls == 1


async def test_five_concurrent_callers_share_one_call() -> None:
    transport = Counting(401)
    answers = await asyncio.gather(
        *(budget.confirm_key_refusal(now=NOW, transport=transport) for _ in range(5))
    )
    assert answers == [True] * 5
    assert transport.calls == 1


async def test_cached_for_60_seconds() -> None:
    transport = Counting(401)
    assert await budget.confirm_key_refusal(now=NOW, transport=transport) is True
    later = NOW + timedelta(seconds=59)
    assert await budget.confirm_key_refusal(now=later, transport=transport) is True
    assert transport.calls == 1
    transport.status = 200
    after = NOW + timedelta(seconds=61)
    assert await budget.confirm_key_refusal(now=after, transport=transport) is False
    assert transport.calls == 2


async def test_no_key_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.delenv("OPENROUTER_API_KEY")
    get_settings.cache_clear()
    transport = Counting(401)
    assert await budget.confirm_key_refusal(now=NOW, transport=transport) is False
    assert transport.calls == 0


async def test_direct_gateway_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("LLM_GATEWAY", "direct")
    get_settings.cache_clear()
    transport = Counting(401)
    assert await budget.confirm_key_refusal(now=NOW, transport=transport) is False
    assert transport.calls == 0
