"""S23 T24 (AC11; D7, C8, C9): the `check_alerts` periodic slot: job error rate, LLM budget,
queue latency and the dead-letter storm summary, each deduped by its window."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, jobs_db
from .test_rollup_jobs import job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
Conn = psycopg.Connection[tuple[Any, ...]]
NOW = datetime(2026, 10, 9, 12, 7, tzinfo=UTC)


def _terminal(conn: Conn, tenant: uuid.UUID, ok: int, failed: int, at: datetime = NOW) -> None:
    started = at - timedelta(minutes=10)
    for _ in range(ok):
        job(conn, tenant, "succeeded", started=started, finished=at - timedelta(minutes=1))
    for _ in range(failed):
        job(conn, tenant, "failed", started=started, finished=at - timedelta(minutes=1))


def _spend(conn: Conn, tenant: uuid.UUID, usd: str, at: datetime = NOW) -> None:
    f.agent_run(conn, tenant, cost_usd=Decimal(usd), created_at=at, started_at=at)


async def _check(now: datetime = NOW) -> dict[str, int]:
    from app.workers.periodic import check_alerts

    return await check_alerts(now)


def _lines(capture: Any, kind: str) -> list[dict[str, Any]]:
    return [line for line in capture.lines() if line["message"] == f"alert.{kind}"]


async def test_alert_events_job_error_rate(jobs_db: JobWorld, log_lines: Any) -> None:
    conn, acme = jobs_db.conn, jobs_db.tenant("acme")
    capture = log_lines()
    _terminal(conn, acme, ok=15, failed=4)  # 19 terminal: under the floor
    assert "job_error_rate" not in await _check()
    _terminal(conn, acme, ok=1, failed=0)  # 20 with 4 failed: 20 % < 25 %
    assert "job_error_rate" not in await _check()
    _terminal(conn, acme, ok=0, failed=1)  # 21 with 5: 23.8 %, still under
    assert "job_error_rate" not in await _check()
    _terminal(conn, acme, ok=0, failed=2)  # 23 with 7: 30 %
    assert (await _check())["job_error_rate"] == 1
    [line] = _lines(capture, "job_error_rate")
    assert (line["job_kind"], line["count"], line["total"], line["rate"]) == (
        "bulk_import",
        "7",
        "23",
        "0.3043",
    )


async def test_alert_events_job_error_rate_exactly_25_pct(jobs_db: JobWorld) -> None:
    _terminal(jobs_db.conn, jobs_db.tenant("acme"), ok=15, failed=5)  # 20 terminal, 5 failed
    assert (await _check())["job_error_rate"] == 1


@pytest.mark.parametrize(
    ("case", "usd", "scope", "pcts"),
    [
        ("tenant_80", "2.000000", "tenant", ["80"]),
        ("tenant_100", "2.500000", "tenant", ["80", "100"]),
        ("global_80", "9.600000", "global", ["80"]),
        ("global_100", "12.000000", "global", ["80", "100"]),
    ],
)
async def test_alert_events_llm_budget(
    jobs_db: JobWorld, log_lines: Any, case: str, usd: str, scope: str, pcts: list[str]
) -> None:
    capture = log_lines()
    if scope == "tenant":
        _spend(jobs_db.conn, jobs_db.tenant("acme"), usd)
    else:  # spread over two tenants so neither tenant's own cap is what fires
        half = (Decimal(usd) / 2).quantize(Decimal("0.000001"))
        _spend(jobs_db.conn, jobs_db.tenant("acme"), str(half))
        _spend(jobs_db.conn, jobs_db.tenant("bharat"), str(Decimal(usd) - half))
    await _check()
    lines = [x for x in _lines(capture, "llm_budget") if x["scope"] == scope]
    assert sorted((x["threshold_pct"] for x in lines), key=int) == pcts
    for line in lines:
        assert line["spend_usd"] == str(Decimal(usd)) or scope == "tenant"
        if scope == "tenant":
            assert line["tenant_id"] == str(jobs_db.tenant("acme"))
            assert line["limit_usd"] == "2.5"


async def test_alert_llm_budget_below_threshold_none(jobs_db: JobWorld) -> None:
    _spend(jobs_db.conn, jobs_db.tenant("acme"), "1.999750")  # 79.99 %
    assert "llm_budget" not in await _check()


async def test_alert_llm_budget_late_row_alerts_next_slot(jobs_db: JobWorld) -> None:
    acme = jobs_db.tenant("acme")
    _spend(jobs_db.conn, acme, "1.0")
    assert "llm_budget" not in await _check()
    _spend(jobs_db.conn, acme, "1.0", at=NOW - timedelta(minutes=3))  # written late
    assert (await _check(NOW + timedelta(minutes=5)))["llm_budget"] == 1


async def test_alert_llm_budget_once_per_threshold_per_day(jobs_db: JobWorld) -> None:
    acme = jobs_db.tenant("acme")
    _spend(jobs_db.conn, acme, "2.0")
    assert (await _check())["llm_budget"] == 1
    assert "llm_budget" not in await _check(NOW + timedelta(minutes=5))
    tomorrow = NOW + timedelta(days=1)
    _spend(jobs_db.conn, acme, "2.0", at=tomorrow)
    assert (await _check(tomorrow))["llm_budget"] == 1


async def test_alert_events_queue_latency(jobs_db: JobWorld, log_lines: Any) -> None:
    conn, acme = jobs_db.conn, jobs_db.tenant("acme")
    capture = log_lines()
    job(conn, acme, "queued", queued=NOW - timedelta(seconds=599))
    assert "queue_latency" not in await _check()
    job(conn, acme, "queued", queued=NOW - timedelta(seconds=600))
    assert (await _check())["queue_latency"] == 1
    assert "queue_latency" not in await _check(NOW + timedelta(minutes=10))  # same 30 min
    [line] = _lines(capture, "queue_latency")
    assert (line["queue"], line["oldest_s"]) == ("default", "600")


async def test_check_alerts_periodic_fires_once_across_two_workers(jobs_db: JobWorld) -> None:
    """Two slots racing (two workers): every condition is claimed once."""
    _terminal(jobs_db.conn, jobs_db.tenant("acme"), ok=10, failed=10)
    _spend(jobs_db.conn, jobs_db.tenant("acme"), "2.0")
    first, second = await asyncio.gather(_check(), _check())
    totals = {k: first.get(k, 0) + second.get(k, 0) for k in set(first) | set(second)}
    assert totals == {"job_error_rate": 1, "llm_budget": 1}


async def test_check_alerts_one_emit_failure_others_still_sent(
    jobs_db: JobWorld, monkeypatch: pytest.MonkeyPatch, log_lines: Any
) -> None:
    from app.workers import periodic

    original = periodic.alert  # type: ignore[attr-defined]

    async def flaky(kind: str, **kwargs: Any) -> bool:
        if kind == "job_error_rate":
            raise RuntimeError("boom")
        return await original(kind, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(periodic, "alert", flaky)
    capture = log_lines()
    _terminal(jobs_db.conn, jobs_db.tenant("acme"), ok=10, failed=10)
    _spend(jobs_db.conn, jobs_db.tenant("acme"), "2.0")
    assert await _check() == {"llm_budget": 1}
    assert "alert.check_failed" in [line["message"] for line in capture.lines()]


async def test_alert_burst_summary_next_slot(jobs_db: JobWorld, log_lines: Any) -> None:
    from app.repositories.rate_limits import claim_cap

    capture = log_lines()
    bucket = "alert:job_dead_lettered:cap:bulk_import"
    closed = datetime(2026, 10, 9, 11, 45, tzinfo=UTC)  # 11:45-12:00, closed at 12:07
    open_ = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    for _ in range(100):
        await claim_cap(bucket, closed)
        await claim_cap(bucket, open_)
    for _ in range(20):
        await claim_cap("alert:job_dead_lettered:cap:brand_embed", closed)  # at the cap: none
    assert (await _check())["job_dead_lettered"] == 1
    [line] = _lines(capture, "job_dead_lettered")
    assert (line["job_kind"], line["count"], line["total"]) == ("bulk_import", "80", "100")
    assert "job_dead_lettered" not in await _check(NOW + timedelta(minutes=5))
