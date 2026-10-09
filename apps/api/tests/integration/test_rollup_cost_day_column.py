"""S23 round-1 regression (R1-1; PLAN AC5): every cost rollup buckets a model call by the UTC day
of `agent_runs.started_at`, the column the kill switch sums (app/repositories/budget.py), never by
`created_at`. A call that starts at 23:59:58Z and is written at 00:00:03Z counts for the earlier
day on the cost page, in the `llm_budget` alert and in the budget refusal alike."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
STARTED = datetime(2026, 10, 4, 23, 59, 58, tzinfo=UTC)
WRITTEN = datetime(2026, 10, 5, 0, 0, 3, tzinfo=UTC)
FOURTH = date(2026, 10, 4)
FIFTH = date(2026, 10, 5)


async def test_spend_day_follows_started_at(products_world: Any) -> None:
    from app.workers.rollup import (
        cost_by_model,
        cost_by_trigger,
        daily_spend,
        rollup_session,
        spend_today,
    )

    conn = products_world.conn
    acme = products_world.tenant("acme")
    f.agent_run(
        conn,
        acme,
        cost_usd=Decimal("1.5"),
        started_at=STARTED,
        finished_at=WRITTEN,
        created_at=WRITTEN,
        model_used="m-midnight",
        trigger="correct",
    )
    async with rollup_session() as session:
        today = await spend_today(session, NOW)
        yesterday = await spend_today(session, STARTED)
        days = await daily_spend(session, NOW, 2)
        models = {d: await cost_by_model(session, d) for d in (FOURTH, FIFTH)}
        triggers = {d: await cost_by_trigger(session, d) for d in (FOURTH, FIFTH)}
    assert today.per_tenant.get(acme, Decimal(0)) == Decimal(0), today
    assert yesterday.per_tenant[acme] == Decimal("1.500000")
    assert {d.day: d.total for d in days} == {FOURTH: Decimal("1.500000"), FIFTH: Decimal(0)}
    assert [(g.key, g.cost_usd) for g in models[FOURTH]] == [("m-midnight", Decimal("1.500000"))]
    assert models[FIFTH] == []
    assert [(g.key, g.cost_usd) for g in triggers[FOURTH]] == [("correct", Decimal("1.500000"))]
    assert triggers[FIFTH] == []


async def test_cost_windows_follow_started_at(products_world: Any) -> None:
    """The 30-day windows (top listings, cost per published listing) open on `started_at`: a
    call started 1 s before the window and written inside it stays out."""
    from app.workers.rollup import cost_per_listing, cost_per_published_listing, rollup_session

    conn = products_world.conn
    acme = products_world.tenant("acme")
    listing = f.listing(conn, acme)
    edge = NOW - timedelta(days=30)
    f.agent_run(
        conn,
        acme,
        cost_usd=Decimal("2"),
        listing_id=listing,
        started_at=edge - timedelta(seconds=1),
        created_at=edge + timedelta(seconds=4),
    )
    async with rollup_session() as session:
        top = await cost_per_listing(session, NOW)
        per = await cost_per_published_listing(session, NOW)
    assert [r.listing_id for r in top] == []
    assert per.spend == Decimal("0.000000")
