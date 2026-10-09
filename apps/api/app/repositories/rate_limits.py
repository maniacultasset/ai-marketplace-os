"""The fixed-window counter (CANON.md:22; ADR-0005:10-16; DATABASE.md:1023-1025).

`increment(bucket_key, window_start)` takes one connection from the app engine, switches it to
AUTOCOMMIT, runs one `INSERT ... ON CONFLICT DO UPDATE ... RETURNING n` and gives the connection
back before the caller opens any business transaction: the counter is never rolled back with the
request's work, and a request never holds two pool connections. `rate_limit_counters` has no
tenant column and no RLS, so this runs before any GUC is known.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import text

from app.db.session import get_engine

_UPSERT: Final = text(
    "INSERT INTO rate_limit_counters (bucket_key, window_start, n) VALUES (:bucket, :window, 1) "
    "ON CONFLICT (bucket_key, window_start) DO UPDATE SET n = rate_limit_counters.n + 1 "
    "RETURNING n"
)


async def increment(bucket_key: str, window_start: datetime) -> int:
    """The bucket's count in this window, after counting this request."""
    async with get_engine().connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        count = await autocommit.scalar(_UPSERT, {"bucket": bucket_key, "window": window_start})
    return int(count)


_PRUNE: Final = text("DELETE FROM rate_limit_counters WHERE window_start < :before")


async def prune(before: datetime) -> int:
    """Delete every counter whose window started before `before` (the 5-minute prune, PLAN
    D12; DATABASE.md:1842). Autocommit, like `increment`; returns the rows deleted."""
    async with get_engine().connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        result = await autocommit.execute(_PRUNE, {"before": before})
    return int(getattr(result, "rowcount", 0) or 0)


# ---- S23: an in-flight cap on the same table (D11, C13) ----

_DECREMENT: Final = text(
    "UPDATE rate_limit_counters SET n = greatest(n - 1, 0) "
    "WHERE bucket_key = :bucket AND window_start = :window"
)


def minute_floor(now: datetime) -> datetime:
    return now.replace(second=0, microsecond=0)


async def _decrement(bucket_key: str, window_start: datetime) -> None:
    async with get_engine().connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(_DECREMENT, {"bucket": bucket_key, "window": window_start})


async def inflight_acquire(bucket_key: str, cap: int, now: datetime) -> datetime | None:
    """Take one of `cap` in-flight slots of `bucket_key`: the minute window's row is counted up,
    and a count above `cap` is counted back down and refused (None). Returns the window to pass
    to `inflight_release`. A request that straddles a minute boundary releases its own row; the
    new window starts at 0 (a brief over-admit, bounded by the request budget)."""
    window = minute_floor(now)
    if await increment(bucket_key, window) > cap:
        await _decrement(bucket_key, window)
        return None
    return window


async def inflight_release(bucket_key: str, window_start: datetime) -> None:
    """Give back the slot `inflight_acquire` took in `window_start` (never below 0)."""
    await _decrement(bucket_key, window_start)
