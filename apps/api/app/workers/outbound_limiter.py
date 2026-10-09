"""The outbound marketplace limiter (spec L10561, L11987, L12001; G17; S20 T13; PLAN D3, D11).

A Postgres fixed-window token bucket per (marketplace, account, operation), shared by every
worker process: bucket `mkt:<marketplace>:<account_id>:<operation>`, 1 s windows, counted by
the same AUTOCOMMIT `rate_limit_counters` increment the API limiter uses (S10 C7), so a count is
never rolled back with business work and never waits on a business lock.

`acquire(operation)` counts one call in the current window; over the operation's rate it sleeps
to the next window start and tries again, at most `max_waits` (30) times, then raises
`RateLimited` (invariant 9: no unbounded wait). It returns the admitted window start (epoch
seconds), so a test can check the rate per window. Time and sleep are injected.

The in-flight bound follows: calls within one adapter session are sequential (D11), so at most
`rate x the longest call` (15 at 1/s with a 15 s worst case) are in flight per account and
operation across all workers.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from mkos_schemas.codes import ErrorCode

from app.adapters.amazon.client import failure
from app.adapters.base import RateLimited
from app.repositories.rate_limits import increment as pg_increment

MAX_WAITS: Final = 30
WINDOW_S: Final = 1.0

Increment = Callable[[str, datetime], Awaitable[int]]


def bucket_key(marketplace: str, account_id: UUID, operation: str) -> str:
    return f"mkt:{marketplace}:{account_id}:{operation}"


class PgOutboundLimiter:
    def __init__(
        self,
        marketplace: str,
        account_id: UUID,
        *,
        rates: Mapping[str, int],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        max_waits: int = MAX_WAITS,
        increment: Increment = pg_increment,
    ) -> None:
        if any(r < 1 for r in rates.values()):
            raise ValueError("every rate is at least 1 call per window")
        self._marketplace = marketplace
        self._account = account_id
        self._rates = dict(rates)
        self._sleep = sleep
        self._clock = clock
        self._max_waits = max_waits
        self._increment = increment

    def __repr__(self) -> str:
        return f"PgOutboundLimiter({self._marketplace!r})"

    async def acquire(self, operation: str) -> float:
        limit = self._rates.get(operation)
        if limit is None:
            raise ValueError("unknown marketplace operation")
        key = bucket_key(self._marketplace, self._account, operation)
        waits = 0
        while True:
            now = self._clock()
            start = math.floor(now / WINDOW_S) * WINDOW_S
            count = await self._increment(key, datetime.fromtimestamp(start, UTC))
            if count <= limit:
                return start
            if waits >= self._max_waits:
                raise RateLimited(
                    failure(ErrorCode.RATE_LIMITED, "The outbound marketplace limit stayed full.")
                )
            waits += 1
            await self._sleep(start + WINDOW_S - now)
