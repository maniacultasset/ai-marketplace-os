"""S18 T7 (AC 14; D10, CP9; DATABASE.md:997-998): the reservation a generation job takes at claim,
the tenant and global caps, and the lock order that keeps concurrent claims under both caps. App
clocks are pinned (`NOW`)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .seam_support import running_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
async def world(products_world: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Ten claims hold ten tenant connections while one more reads the global sum."""
    from app.core.config import get_settings
    from app.db.session import dispose_engine

    monkeypatch.setenv("DATABASE_POOL_SIZE", "12")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "4")
    get_settings.cache_clear()
    await dispose_engine()
    return products_world


def _reserved(conn: Any, job: uuid.UUID) -> Decimal:
    row = conn.execute("SELECT cost_reserved_usd FROM job_runs WHERE id = %s", [job]).fetchone()
    return Decimal(row[0])


async def _reserve(tenant: uuid.UUID, job: uuid.UUID, attempt: int = 1) -> Any:
    from app.workers.budget import reserve

    return await reserve(tenant, job, attempt, now=NOW)


async def test_reserve_sets_column(world: Any) -> None:
    acme = world.tenant("acme")
    job = running_job(world.conn, acme)
    assert await _reserve(acme, job) is True
    assert _reserved(world.conn, job) == Decimal("0.02")


def test_reservation_constant() -> None:
    from app.workers.budget import GENERATION_RESERVATION_USD

    assert Decimal("0.02") == GENERATION_RESERVATION_USD


async def test_reserve_over_tenant_cap_refused(world: Any) -> None:
    from app.workers.budget import BudgetRefused

    acme = world.tenant("acme")
    f.agent_run(world.conn, acme, cost_usd=Decimal("2.49"), started_at="2026-10-05T01:00:00Z")
    job = running_job(world.conn, acme)
    with pytest.raises(BudgetRefused) as caught:
        await _reserve(acme, job)
    assert caught.value.scope == "tenant"
    assert _reserved(world.conn, job) == Decimal("0")


async def test_reserve_over_global_cap_refused(world: Any) -> None:
    from app.workers.budget import BudgetRefused

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    world.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 100 WHERE id = %s", [bharat])
    f.agent_run(world.conn, bharat, cost_usd=Decimal("11.99"), started_at="2026-10-05T01:00:00Z")
    job = running_job(world.conn, acme)
    with pytest.raises(BudgetRefused) as caught:
        await _reserve(acme, job)
    assert caught.value.scope == "global"


async def _race(pairs: list[tuple[uuid.UUID, uuid.UUID]]) -> tuple[int, int]:
    from app.workers.budget import BudgetRefused

    results = await asyncio.gather(*(_reserve(t, j) for t, j in pairs), return_exceptions=True)
    others = [r for r in results if r is not True and not isinstance(r, BudgetRefused)]
    assert others == [], others
    return sum(r is True for r in results), sum(isinstance(r, BudgetRefused) for r in results)


async def test_reserve_10_concurrent_never_exceed_cap(world: Any) -> None:
    acme = world.tenant("acme")
    world.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 0.07 WHERE id = %s", [acme])
    jobs = [running_job(world.conn, acme) for _ in range(10)]
    assert await _race([(acme, j) for j in jobs]) == (3, 7)
    assert sum(_reserved(world.conn, j) for j in jobs) == Decimal("0.06")


async def test_reserve_concurrent_tenants_never_exceed_global_cap(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "0.07")
    get_settings.cache_clear()
    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    world.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 100")
    pairs = [(acme, running_job(world.conn, acme)) for _ in range(5)]
    pairs += [(bharat, running_job(world.conn, bharat)) for _ in range(5)]
    assert await asyncio.wait_for(_race(pairs), 60) == (3, 7)
    assert sum(_reserved(world.conn, j) for _, j in pairs) == Decimal("0.06")


async def test_reserve_fenced_on_attempt(world: Any) -> None:
    acme = world.tenant("acme")
    job = running_job(world.conn, acme, attempt=1)
    assert await _reserve(acme, job, attempt=2) is False
    assert _reserved(world.conn, job) == Decimal("0")


# ---- SI10 T6: the credit row refuses before any lock or write ----


def _pause(conn: Any, reason: str) -> None:
    conn.execute(
        "UPDATE ai_credit_state SET paused_reason = %s, paused_at = %s WHERE id = 1", [reason, NOW]
    )


def _held_locks(conn: Any) -> int:
    return int(
        conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND database = "
            "(SELECT oid FROM pg_database WHERE datname = current_database())"
        ).fetchone()[0]
    )


@pytest.mark.parametrize("reason", ["credit_exhausted", "key_invalid"])
async def test_reserve_refused_while_paused(
    world: Any, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.budget import BudgetRepository
    from app.workers.budget import BudgetRefused

    taken: list[str] = []

    async def spy_lock(self: Any) -> None:
        taken.append("tenant")

    monkeypatch.setattr(BudgetRepository, "lock_tenant", spy_lock)
    acme = world.tenant("acme")
    _pause(world.conn, reason)
    job = running_job(world.conn, acme)
    with pytest.raises(BudgetRefused) as caught:
        await _reserve(acme, job)
    assert (caught.value.scope, caught.value.reason) == ("credit", reason)
    assert taken == [], "no budget lock is taken"
    assert _reserved(world.conn, job) == Decimal("0")
    assert _held_locks(world.conn) == 0


async def test_reserve_refused_fresh_low_admits_stale_low(world: Any) -> None:
    from app.workers.budget import BudgetRefused

    acme = world.tenant("acme")
    world.conn.execute(
        "UPDATE ai_credit_state SET remaining_usd = 0.40, checked_at = %s WHERE id = 1",
        [NOW - timedelta(minutes=5)],
    )
    with pytest.raises(BudgetRefused) as caught:
        await _reserve(acme, running_job(world.conn, acme))
    assert (caught.value.scope, caught.value.reason) == ("credit", "credit_exhausted")
    world.conn.execute(
        "UPDATE ai_credit_state SET checked_at = %s WHERE id = 1", [NOW - timedelta(minutes=16)]
    )
    assert await _reserve(acme, running_job(world.conn, acme)) is True


# ---- SI10 T6: pause_ai ----


class AlertSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, **kw: Any) -> bool:
        self.calls.append((kind, kw))
        return True


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> AlertSpy:
    from app.workers import budget

    spy = AlertSpy()
    monkeypatch.setattr(budget, "alert", spy)
    return spy


def _row(conn: Any) -> tuple[Any, ...]:
    return tuple(
        conn.execute("SELECT paused_reason, paused_at FROM ai_credit_state WHERE id = 1").fetchone()
    )


async def test_pause_ai_first_wins_one_alert(world: Any, alerts: AlertSpy) -> None:
    from app.workers.budget import pause_ai

    job = uuid.uuid4()
    assert await pause_ai("credit_exhausted", job_id=job, now=NOW) is True
    first = _row(world.conn)
    assert first == ("credit_exhausted", NOW)
    later = NOW + timedelta(minutes=3)
    assert await pause_ai("key_invalid", job_id=None, now=later) is False
    assert _row(world.conn) == first
    assert len(alerts.calls) == 1
    kind, kw = alerts.calls[0]
    assert kind == "llm_provider_refused"
    assert kw["key"] == "credit_exhausted"
    assert kw["level"] == "error"
    assert kw["error_code"] == "AI_CREDIT_EXHAUSTED"
    assert kw["job_id"] == job
    assert kw["window_start"] == NOW.replace(minute=0)


async def test_pause_ai_same_reason_twice_keeps_first(world: Any, alerts: AlertSpy) -> None:
    from app.workers.budget import pause_ai

    assert await pause_ai("credit_exhausted", now=NOW) is True
    assert await pause_ai("credit_exhausted", now=NOW + timedelta(minutes=7)) is False
    assert _row(world.conn) == ("credit_exhausted", NOW)
    assert len(alerts.calls) == 1


async def test_pause_ai_db_error_logged_not_raised(
    world: Any, alerts: AlertSpy, log_lines: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.ai_credit import AiCreditRepository
    from app.workers.budget import pause_ai

    async def boom(self: Any, reason: str, at: datetime) -> bool:
        raise RuntimeError("db down")

    monkeypatch.setattr(AiCreditRepository, "pause", boom)
    capture = log_lines()
    assert await pause_ai("credit_exhausted", now=NOW) is False
    failed = [line for line in capture.lines() if line["message"] == "ai_credit.pause_failed"]
    assert len(failed) == 1 and failed[0]["severity"] == "ERROR"
    assert failed[0]["error_class"] == "RuntimeError"
    assert alerts.calls == []
    assert _row(world.conn) == (None, None)
