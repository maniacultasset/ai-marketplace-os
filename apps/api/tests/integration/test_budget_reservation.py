"""S18 T7 (AC 14; D10, CP9; DATABASE.md:997-998): the reservation a generation job takes at claim,
the tenant and global caps, and the lock order that keeps concurrent claims under both caps. App
clocks are pinned (`NOW`)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
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
    assert _reserved(world.conn, job) == Decimal("0.10")


async def test_reserve_over_tenant_cap_refused(world: Any) -> None:
    from app.workers.budget import BudgetRefused

    acme = world.tenant("acme")
    f.agent_run(world.conn, acme, cost_usd=Decimal("2.45"), started_at="2026-10-05T01:00:00Z")
    job = running_job(world.conn, acme)
    with pytest.raises(BudgetRefused) as caught:
        await _reserve(acme, job)
    assert caught.value.scope == "tenant"
    assert _reserved(world.conn, job) == Decimal("0")


async def test_reserve_over_global_cap_refused(world: Any) -> None:
    from app.workers.budget import BudgetRefused

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    world.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 100 WHERE id = %s", [bharat])
    f.agent_run(world.conn, bharat, cost_usd=Decimal("11.95"), started_at="2026-10-05T01:00:00Z")
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
    world.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 0.35 WHERE id = %s", [acme])
    jobs = [running_job(world.conn, acme) for _ in range(10)]
    assert await _race([(acme, j) for j in jobs]) == (3, 7)
    assert sum(_reserved(world.conn, j) for j in jobs) == Decimal("0.30")


async def test_reserve_concurrent_tenants_never_exceed_global_cap(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "0.35")
    get_settings.cache_clear()
    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    world.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 100")
    pairs = [(acme, running_job(world.conn, acme)) for _ in range(5)]
    pairs += [(bharat, running_job(world.conn, bharat)) for _ in range(5)]
    assert await asyncio.wait_for(_race(pairs), 60) == (3, 7)
    assert sum(_reserved(world.conn, j) for _, j in pairs) == Decimal("0.30")


async def test_reserve_fenced_on_attempt(world: Any) -> None:
    acme = world.tenant("acme")
    job = running_job(world.conn, acme, attempt=1)
    assert await _reserve(acme, job, attempt=2) is False
    assert _reserved(world.conn, job) == Decimal("0")
