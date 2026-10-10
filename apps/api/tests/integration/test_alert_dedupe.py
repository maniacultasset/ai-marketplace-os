"""S23 T21 (AC11; C2, C3): the alert dedupe claim, the dead-letter cap counter and the over-cap
scan on `rate_limit_counters`, and the prune keeping `alert:` rows for 25 h."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
Conn = psycopg.Connection[tuple[Any, ...]]
DAY = datetime(2026, 10, 9, tzinfo=UTC)


async def test_alert_dedupe(jobs_db: JobWorld) -> None:
    from app.repositories.rate_limits import claim_alert

    assert await claim_alert("llm_budget", "tenant:t1:80", DAY) is True
    assert await claim_alert("llm_budget", "tenant:t1:80", DAY) is False


async def test_alert_dedupe_next_window_true(jobs_db: JobWorld) -> None:
    from app.repositories.rate_limits import claim_alert

    assert await claim_alert("llm_budget", "global:80", DAY)
    assert await claim_alert("llm_budget", "global:80", DAY + timedelta(days=1))


async def test_alert_dedupe_concurrent_claims_one_wins(jobs_db: JobWorld) -> None:
    from app.repositories.rate_limits import claim_alert

    results = await asyncio.gather(
        *(claim_alert("queue_latency", "default", DAY) for _ in range(5))
    )
    assert sorted(results) == [False, False, False, False, True]


async def test_alert_dedupe_other_key_independent(jobs_db: JobWorld) -> None:
    from app.repositories.rate_limits import claim_alert

    assert await claim_alert("queue_latency", "default", DAY)
    assert await claim_alert("queue_latency", "publish", DAY)
    assert await claim_alert("job_error_rate", "default", DAY)


async def test_claim_cap_counts_concurrently(jobs_db: JobWorld) -> None:
    from app.repositories.rate_limits import claim_cap

    bucket = "alert:job_dead_lettered:cap:bulk_import"
    counts = await asyncio.gather(*(claim_cap(bucket, DAY) for _ in range(30)))
    assert sorted(counts) == list(range(1, 31))
    with pytest.raises(ValueError, match="alert"):
        await claim_cap("t:other", DAY)


async def test_over_cap_closed_windows_only(jobs_db: JobWorld) -> None:
    from app.repositories.rate_limits import claim_cap, over_cap

    prefix = "alert:job_dead_lettered:cap:"
    now = datetime(2026, 10, 9, 12, 20, tzinfo=UTC)
    closed, open_ = now - timedelta(minutes=20), now - timedelta(minutes=5)
    for _ in range(25):
        await claim_cap(prefix + "bulk_import", closed)
        await claim_cap(prefix + "bulk_import", open_)
    for _ in range(20):
        await claim_cap(prefix + "brand_embed", closed)  # at the limit: not over
    window_end = now - timedelta(minutes=15)
    rows = await over_cap(prefix, 20, closed_before=window_end, since=now - timedelta(hours=2))
    assert rows == [(prefix + "bulk_import", closed, 25)]
    with pytest.raises(ValueError):
        await over_cap("t:", 1, closed_before=now, since=closed)


def _insert(conn: Conn, key: str, at: datetime) -> None:
    conn.execute(
        "INSERT INTO rate_limit_counters (bucket_key, window_start, n) VALUES (%s, %s, 1)",
        [key, at],
    )


async def test_prune_keeps_alert_rows_25h(jobs_db: JobWorld) -> None:
    from app.workers.periodic import prune_rate_limits

    now = datetime.now(UTC)
    _insert(jobs_db.conn, "alert:llm_budget:global:80", now - timedelta(hours=24))
    _insert(jobs_db.conn, "alert:job_dead_lettered:cap:bulk_import", now - timedelta(hours=2))
    _insert(jobs_db.conn, "alert:job_dead_lettered:summary:bulk_import", now - timedelta(hours=3))
    _insert(jobs_db.conn, "alert:queue_latency:default", now - timedelta(hours=26))
    _insert(jobs_db.conn, "t:limiter", now - timedelta(minutes=6))
    assert await prune_rate_limits(now) == 2
    keys = {r[0] for r in jobs_db.conn.execute("SELECT bucket_key FROM rate_limit_counters")}
    assert keys == {
        "alert:llm_budget:global:80",
        "alert:job_dead_lettered:cap:bulk_import",
        "alert:job_dead_lettered:summary:bulk_import",
    }


async def test_prune_still_drops_old_limiter_rows(jobs_db: JobWorld) -> None:
    from app.workers.periodic import prune_rate_limits

    now = datetime.now(UTC)
    for minutes in (0, 4, 6, 60):
        _insert(jobs_db.conn, f"b{minutes}", now - timedelta(minutes=minutes))
    assert await prune_rate_limits(now) == 2
    keys = {r[0] for r in jobs_db.conn.execute("SELECT bucket_key FROM rate_limit_counters")}
    assert keys == {"b0", "b4"}
