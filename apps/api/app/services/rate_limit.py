"""Rate limiting (CANON.md:22, :73; ADR-0005:10-16): fixed one-minute windows in Postgres.

`hit(bucket, limit)` counts one request in the bucket's current window and returns the count;
the API layer answers 429 once it passes `limit`. Bucket keys: `t:<tenant>` (120/min, the
verified access token's tenant, counted once its membership probe passes), `u:<tenant>:<user>`
(the same limit, for a verified token whose probe failed), `ip:<client ip>` (60/min, public routes) and
`login:<sha256(ip|lower(email))>` (5/min, the login).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.repositories.rate_limits import increment


def now() -> datetime:
    return datetime.now(UTC)


def window_start(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def seconds_left(moment: datetime) -> int:
    """Seconds until the next window (the `Retry-After` value), at least 1."""
    return max(1, 60 - moment.second)


async def hit(bucket: str, limit: int) -> int:
    """Count one request in `bucket`'s window; the new count (compare it with `limit`)."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return await increment(bucket, window_start(now()))
