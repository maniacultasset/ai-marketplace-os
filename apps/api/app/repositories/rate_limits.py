"""The fixed-window counter (CANON.md:22; ADR-0005:10-16; DATABASE.md:1023-1025).

`increment(bucket_key, window_start)` takes one connection from the app engine, switches it to
AUTOCOMMIT, runs one `INSERT ... ON CONFLICT DO UPDATE ... RETURNING n` and gives the connection
back before the caller opens any business transaction: the counter is never rolled back with the
request's work, and a request never holds two pool connections. `rate_limit_counters` has no
tenant column and no RLS, so this runs before any GUC is known.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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


# S23 (C3): `alert:` rows (dedupe claims, cap and summary counters) outlive the limiter windows:
# kept until their window started more than 25 h ago, so a day-long dedupe holds all day.
ALERT_PREFIX: Final = "alert:"
ALERT_KEEP: Final = timedelta(hours=25)
_PRUNE: Final = text(
    "DELETE FROM rate_limit_counters WHERE "
    "(bucket_key NOT LIKE 'alert:%' AND window_start < :before) "
    "OR (bucket_key LIKE 'alert:%' AND window_start < :alert_before)"
)


async def prune(before: datetime, *, alert_before: datetime | None = None) -> int:
    """Delete every limiter counter whose window started before `before` (the 5-minute prune,
    PLAN D12; DATABASE.md:1842) and every `alert:` row older than `alert_before` (default
    `before - 25 h`). Autocommit, like `increment`; returns the rows deleted."""
    alert_cut = alert_before if alert_before is not None else before - ALERT_KEEP
    async with get_engine().connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        result = await autocommit.execute(_PRUNE, {"before": before, "alert_before": alert_cut})
    return int(getattr(result, "rowcount", 0) or 0)


# ---- S23: the alert dedupe and the dead-letter cap (C2, C3) ----


async def claim_alert(kind: str, key: str, window_start: datetime) -> bool:
    """True for the first claim of `alert:<kind>:<key>` in the window, False for every repeat
    (one autocommit upsert: concurrent claimants serialise on the row, exactly one wins)."""
    return await increment(f"{ALERT_PREFIX}{kind}:{key}", window_start) == 1


async def claim_cap(bucket: str, window_start: datetime) -> int:
    """Count one event in `bucket`'s window; returns the window's count including it."""
    if not bucket.startswith(ALERT_PREFIX):
        raise ValueError("a cap bucket is an alert: bucket")
    return await increment(bucket, window_start)


_OVER_CAP: Final = text(
    "SELECT bucket_key, window_start, n FROM rate_limit_counters "
    "WHERE left(bucket_key, :prefix_len) = :prefix AND n > :limit "
    "AND window_start < :closed_before AND window_start >= :since "
    "ORDER BY window_start, bucket_key"
)


async def over_cap(
    prefix: str, limit: int, *, closed_before: datetime, since: datetime
) -> list[tuple[str, datetime, int]]:
    """Cap windows under `prefix` whose count passed `limit`, started in `[since,
    closed_before)` (a window still open is never returned)."""
    if not prefix.startswith(ALERT_PREFIX):
        raise ValueError("over_cap reads alert: buckets only")
    async with get_engine().connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        rows = await autocommit.execute(
            _OVER_CAP,
            {
                "prefix": prefix,
                "prefix_len": len(prefix),
                "limit": limit,
                "closed_before": closed_before,
                "since": since,
            },
        )
    return [(str(b), w, int(n)) for b, w, n in rows]


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
