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
