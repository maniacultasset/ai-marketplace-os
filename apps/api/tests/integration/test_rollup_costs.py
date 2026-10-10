"""S23 T9 (AC5, AC6; KNOWN-ISSUES.md:31): `app/workers/rollup.py` cost rollups, read through
`platform_scope()` as `app_platform_ro`, over fixed `agent_runs` rows of two tenants."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

Conn = psycopg.Connection[tuple[Any, ...]]
DAY = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
LATE = datetime(2026, 10, 4, 23, 59, 59, tzinfo=UTC)  # the day before (05:29 IST on the 5th)
EARLY = datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC)


@dataclass
class CostWorld:
    conn: Conn
    acme: UUID
    bharat: UUID
    listings: dict[str, UUID]


def _run(conn: Conn, tenant: UUID, cost: str, at: datetime, **extra: Any) -> None:
    """A call started at `at` and written 5 s later: the rollups read `started_at` (R1-1)."""
    written = at + timedelta(seconds=5)
    f.agent_run(conn, tenant, cost_usd=Decimal(cost), started_at=at, created_at=written, **extra)


@pytest.fixture
def world(products_world: Any) -> CostWorld:
    conn = products_world.conn
    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    conn.execute("UPDATE tenants SET name = 'Acme <b>&co</b>' WHERE id = %s", [acme])
    listings = {
        "a1": f.listing(conn, acme),
        "a2": f.listing(conn, acme),
        "b1": f.listing(conn, bharat),
    }
    # a1: 0.000001 + 0.000002 + 0.5 (exact numeric sum); a NULL-listing run is never a listing's
    _run(conn, acme, "0.000001", EARLY, listing_id=listings["a1"], model_used="m-small")
    _run(conn, acme, "0.000002", EARLY, listing_id=listings["a1"], model_used="m-small")
    _run(conn, acme, "0.5", NOW, listing_id=listings["a1"], model_used="m-big", trigger="correct")
    _run(conn, acme, "0.25", NOW, model_used="m-embed", trigger="embed", agent_name="embedding")
    # a2 ties b1 at 0.3: ordered by listing id
    _run(conn, acme, "0.3", NOW, listing_id=listings["a2"], model_used="m-big")
    _run(conn, bharat, "0.3", NOW, listing_id=listings["b1"], model_used="m-big")
    # the day before, 23:59:59Z: counts for the 4th, never the 5th
    _run(conn, bharat, "9", LATE, listing_id=listings["b1"], model_used="m-big")
    return CostWorld(conn, acme, bharat, listings)


async def _ro[T](fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    from app.workers.rollup import rollup_session

    async with rollup_session() as session:
        return await fn(session, *args, **kwargs)


async def test_cost_rollup(world: CostWorld) -> None:
    from app.workers.rollup import listing_cost

    a1 = await _ro(listing_cost, world.listings["a1"])
    assert a1 is not None
    assert (a1.cost_usd, a1.runs, a1.tenant_id) == (Decimal("0.500003"), 3, world.acme)
    rows = world.conn.execute(
        "SELECT sum(cost_usd) FROM agent_runs WHERE listing_id = %s", [world.listings["a1"]]
    ).fetchone()
    assert rows is not None and a1.cost_usd == rows[0]
    b1 = await _ro(listing_cost, world.listings["b1"])
    assert b1 is not None and b1.cost_usd == Decimal("9.300000")


async def test_cost_rollup_listing_without_runs_is_zero(world: CostWorld) -> None:
    from app.workers.rollup import listing_cost

    fresh = f.listing(world.conn, world.acme)
    got = await _ro(listing_cost, fresh)
    assert got is not None and (got.cost_usd, got.runs) == (Decimal("0.000000"), 0)


async def test_cost_rollup_per_tenant_utc_day(world: CostWorld) -> None:
    from app.workers.rollup import daily_spend, spend_today

    today = await _ro(spend_today, NOW)
    assert today.per_tenant == {world.acme: Decimal("1.050003"), world.bharat: Decimal("0.300000")}
    assert today.global_usd == Decimal("1.350003")
    days = await _ro(daily_spend, NOW, 2)
    assert [(d.day, d.total) for d in days] == [
        (date(2026, 10, 4), Decimal("9.000000")),
        (date(2026, 10, 5), Decimal("1.350003")),
    ]


async def test_cost_rollup_per_model(world: CostWorld) -> None:
    from app.workers.rollup import GroupCost, cost_by_model

    assert await _ro(cost_by_model, DAY) == [
        GroupCost("m-big", Decimal("1.100000"), 3),
        GroupCost("m-embed", Decimal("0.250000"), 1),
        GroupCost("m-small", Decimal("0.000003"), 2),
    ]


async def test_cost_rollup_per_trigger(world: CostWorld) -> None:
    from app.workers.rollup import GroupCost, cost_by_trigger

    assert await _ro(cost_by_trigger, DAY) == [
        GroupCost("generate", Decimal("0.600003"), 4),
        GroupCost("correct", Decimal("0.500000"), 1),
        GroupCost("embed", Decimal("0.250000"), 1),
    ]


async def test_cost_rollup_top_50_order_and_ties(world: CostWorld) -> None:
    from app.workers.rollup import cost_per_listing

    rows = await _ro(cost_per_listing, NOW)
    # b1's 9.0 of the 4th is inside 30 days too
    assert [r.listing_id for r in rows] == [
        world.listings["b1"],
        world.listings["a1"],
        world.listings["a2"],
    ]
    assert rows[0].cost_usd == Decimal("9.300000")
    tied = [world.listings["a2"]]
    for _ in range(55):
        listing = f.listing(world.conn, world.acme)
        _run(world.conn, world.acme, "0.3", NOW, listing_id=listing, model_used="m")
        tied.append(listing)
    rows = await _ro(cost_per_listing, NOW)
    assert len(rows) == 50
    assert [r.listing_id for r in rows[:2]] == [world.listings["b1"], world.listings["a1"]]
    # the 48 places left go to the 0.3 ties, lowest listing id first
    assert [r.listing_id for r in rows[2:]] == sorted(tied)[:48]
    with pytest.raises(ValueError, match="limit"):
        await _ro(cost_per_listing, NOW, limit=51)


async def test_cost_rollup_unknown_listing_none(world: CostWorld) -> None:
    import uuid

    from app.workers.rollup import listing_cost

    assert await _ro(listing_cost, uuid.uuid4()) is None


async def test_rollup_read_only(world: CostWorld) -> None:
    from app.workers.rollup import rollup_session
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async with rollup_session() as session:
        with pytest.raises(DBAPIError) as caught:
            await session.execute(
                text("INSERT INTO rate_limit_counters VALUES ('rollup-probe', now(), 1)")
            )
    assert getattr(caught.value.orig, "sqlstate", None) == "25006"


async def test_rollup_runs_as_platform_ro(world: CostWorld) -> None:
    from sqlalchemy import text

    async def whoami(session: Any) -> Any:
        return (await session.execute(text("SELECT current_user"))).scalar()

    assert await _ro(whoami) == "app_platform_ro"


async def test_admin_ops_reexports_rollup(world: CostWorld) -> None:
    """The admin's cost numbers are the rollup module's (one copy of the SQL)."""
    from app.admin import ops
    from app.workers import rollup

    assert ops.daily_spend is rollup.daily_spend
    assert ops.cost_per_published_listing is rollup.cost_per_published_listing


def test_rollup_naive_now_refused() -> None:
    from app.workers.rollup import aware_utc

    with pytest.raises(ValueError, match="aware"):
        aware_utc(datetime(2026, 10, 5))
