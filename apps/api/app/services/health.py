"""Readiness: one in-flight `SELECT 1` per instance, with its result cached briefly.

`/readyz` is unauthenticated and, from S9, shares the pool with business routes. Concurrent
probes share one in-flight check, and each result (success or failure) is reused for
`READINESS_CACHE_SECONDS`, so a probe flood costs at most one query per second per instance.
This is the only `app.services` module allowed to import `app.db.session` (.importlinter).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Final

from app.db.session import ping

READINESS_TIMEOUT_SECONDS: Final[float] = 2.0
READINESS_CACHE_SECONDS: Final[float] = 1.0


class ReadinessCheck:
    """Per-app (so per-instance: one uvicorn process per instance, ADR-0005) readiness state."""

    def __init__(
        self, *, ttl: float = READINESS_CACHE_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._inflight: asyncio.Task[bool] | None = None
        self._result: bool | None = None
        self._checked_at = 0.0

    async def is_ready(self) -> bool:
        if self._inflight is not None:
            # A cancelled waiter must never cancel the check the other waiters share.
            return await asyncio.shield(self._inflight)
        if self._result is not None and self._clock() - self._checked_at < self._ttl:
            return self._result
        self._inflight = asyncio.create_task(self._check())
        return await asyncio.shield(self._inflight)

    async def _check(self) -> bool:
        try:
            result = await ping(READINESS_TIMEOUT_SECONDS)
            self._result = result
            self._checked_at = self._clock()
            return result
        finally:
            self._inflight = None
