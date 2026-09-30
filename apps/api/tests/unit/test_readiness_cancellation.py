"""AC6b (added by the e2e tester): a probe whose client goes away (its waiter is cancelled) must
never cancel the readiness check that concurrent probes share (the `asyncio.shield` in
`ReadinessCheck.is_ready`, CODE-PLAN.md T5)."""

from __future__ import annotations

import asyncio

import pytest


class SlowPing:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()

    async def __call__(self, timeout_seconds: float) -> bool:
        self.calls += 1
        self.started.set()
        await asyncio.sleep(0.05)
        return True


@pytest.mark.parametrize("cancelled", ["first", "joiner"])
async def test_cancelled_waiter_never_cancels_the_shared_check(
    monkeypatch: pytest.MonkeyPatch, cancelled: str
) -> None:
    from app.services.health import ReadinessCheck

    ping = SlowPing()
    monkeypatch.setattr("app.services.health.ping", ping)
    check = ReadinessCheck()

    first = asyncio.create_task(check.is_ready())
    await ping.started.wait()
    joiners = [asyncio.create_task(check.is_ready()) for _ in range(3)]
    await asyncio.sleep(0)

    victim = first if cancelled == "first" else joiners[0]
    victim.cancel()
    survivors = [task for task in (first, *joiners) if task is not victim]
    results = await asyncio.gather(*survivors)

    assert results == [True] * len(survivors)
    assert ping.calls == 1
    with pytest.raises(asyncio.CancelledError):
        await victim
