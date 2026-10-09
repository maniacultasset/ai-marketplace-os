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
from .jobs_support import JobWorld, insert_job, jobs_db
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


def _cap(conn: Conn, tenant: uuid.UUID, usd: str) -> None:
    conn.execute("UPDATE tenants SET llm_daily_budget_usd = %s WHERE id = %s", [usd, tenant])


def _reserve(conn: Conn, tenant: uuid.UUID, usd: str = "0.10", at: datetime = NOW) -> None:
    """A claimed `listing_generate` attempt holding a reservation (what the kill switch counts)."""
    listing = f.listing(conn, tenant)
    insert_job(
        conn,
        tenant,
        kind="listing_generate",
        entity_type="listing",
        entity_id=listing,
        listing_id=listing,
        status="running",
        started_at=at,
        lease_expires_at=at + timedelta(hours=1),
        cost_reserved_usd=Decimal(usd),
    )


def _tenant_lines(capture: Any) -> list[tuple[str, str, str]]:
    return sorted(
        (x["threshold_pct"], x["spend_usd"], x["limit_usd"])
        for x in _lines(capture, "llm_budget")
        if x["scope"] == "tenant"
    )


async def test_alert_llm_budget_tenant_override_low(jobs_db: JobWorld, log_lines: Any) -> None:
    """R1-2: a tenant capped at 1.00 alerts at 0.80 and 1.00, the cap the kill switch enforces."""
    acme = jobs_db.tenant("acme")
    _cap(jobs_db.conn, acme, "1.00")
    capture = log_lines()
    _spend(jobs_db.conn, acme, "0.8")
    assert (await _check())["llm_budget"] == 1
    _spend(jobs_db.conn, acme, "0.2")
    assert (await _check(NOW + timedelta(minutes=5)))["llm_budget"] == 1
    assert _tenant_lines(capture) == [
        ("100", "1.000000", "1.00"),
        ("80", "0.800000", "1.00"),
    ]
    assert {x["tenant_id"] for x in _lines(capture, "llm_budget")} == {str(acme)}


async def test_alert_llm_budget_tenant_override_high_no_false_alert(jobs_db: JobWorld) -> None:
    """R1-2: a tenant capped at 20.00 spending the 2.50 default is at 12.5 %: no alert."""
    acme = jobs_db.tenant("acme")
    _cap(jobs_db.conn, acme, "20.00")
    _spend(jobs_db.conn, acme, "2.5")
    assert "llm_budget" not in await _check()


async def test_alert_llm_budget_counts_reservations(jobs_db: JobWorld, log_lines: Any) -> None:
    """R1-2: spend 2.30 plus two live 0.10 reservations is the 2.50 the kill switch refuses at,
    so both thresholds fire; the event states the spend alone."""
    from app.workers.budget import BudgetRefused, reserve

    conn, acme = jobs_db.conn, jobs_db.tenant("acme")
    capture = log_lines()
    _spend(conn, acme, "2.3")
    _reserve(conn, acme)
    _reserve(conn, acme)
    with pytest.raises(BudgetRefused):  # the kill switch already refuses the next claim
        await reserve(acme, uuid.uuid4(), 1, now=NOW)
    assert (await _check())["llm_budget"] == 2
    assert _tenant_lines(capture) == [("100", "2.300000", "2.5"), ("80", "2.300000", "2.5")]


async def test_alert_llm_budget_reservations_only_tenant(jobs_db: JobWorld) -> None:
    """R1-2: a tenant with reservations and no spend yet is still checked."""
    acme = jobs_db.tenant("acme")
    _cap(jobs_db.conn, acme, "0.20")
    _reserve(jobs_db.conn, acme, "0.20")
    assert (await _check())["llm_budget"] == 2


async def test_alert_llm_budget_global_counts_reservations(
    jobs_db: JobWorld, log_lines: Any
) -> None:
    conn = jobs_db.conn
    for name in ("acme", "bharat"):
        tenant = jobs_db.tenant(name)
        _cap(conn, tenant, "100")
        _spend(conn, tenant, "4.7")
        _reserve(conn, tenant, "0.10")  # 9.4 spent + 0.2 reserved = 9.6 = 80 % of 12
    capture = log_lines()
    await _check()
    [line] = [x for x in _lines(capture, "llm_budget") if x["scope"] == "global"]
    assert (line["threshold_pct"], line["spend_usd"]) == ("80", "9.400000")


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
