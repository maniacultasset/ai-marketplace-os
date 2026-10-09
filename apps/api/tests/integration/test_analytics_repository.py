"""S23 T4-T5: `AnalyticsRepository` over the analytics world, as `app_rw` (RLS on)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from .analytics_support import (
    AFTER,
    BEFORE,
    EXPECTED,
    FIRST,
    FROM,
    LAST,
    TO,
    AnalyticsWorld,
    build_analytics_world,
)
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

Conn = psycopg.Connection[tuple[Any, ...]]


@pytest.fixture
def world(auth_db: Conn) -> AnalyticsWorld:
    return build_analytics_world(auth_db)


def _bounds() -> tuple[datetime, datetime]:
    from app.services.analytics import DateRange

    return DateRange(FROM, TO).bounds()


async def _read[T](tenant: UUID, fn: Callable[[Any], Awaitable[T]]) -> T:
    from app.repositories.analytics import AnalyticsRepository
    from app.repositories.transactions import unit_of_work

    async with unit_of_work(tenant) as session:
        return await fn(AnalyticsRepository(session, tenant))


def _expected(slug: str, *keys: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for marketplace, per in EXPECTED[slug].items():
        values = tuple(per[k] for k in keys)
        if any(values):
            out[marketplace] = values[0] if len(values) == 1 else values
    return out


@pytest.mark.parametrize("slug", ["acme", "bharat"])
async def test_analytics_metrics_volume_golden(world: AnalyticsWorld, slug: str) -> None:
    lo, hi = _bounds()
    got = await _read(world.tenants[slug], lambda r: r.volume(lo, hi))
    assert got == _expected(slug, "listings_generated")


@pytest.mark.parametrize("slug", ["acme", "bharat"])
async def test_analytics_metrics_decisions_golden(world: AnalyticsWorld, slug: str) -> None:
    lo, hi = _bounds()
    got = await _read(world.tenants[slug], lambda r: r.decisions(lo, hi))
    assert got == _expected(slug, "approvals", "rejections")


async def test_analytics_boundaries_volume(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    # only the edge rows: [FIRST, FIRST+1µs) and [LAST, AFTER)
    tick = timedelta(microseconds=1)
    assert await _read(acme, lambda r: r.volume(FIRST, FIRST + tick)) == {"amazon": 1}
    assert await _read(acme, lambda r: r.volume(LAST, AFTER)) == {"amazon": 1}
    assert await _read(acme, lambda r: r.volume(BEFORE, FIRST)) == {"amazon": 1}
    assert await _read(acme, lambda r: r.volume(AFTER, AFTER + tick)) == {"amazon": 1}


async def test_analytics_excludes_edit_and_import(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    day10, day12 = datetime.fromisoformat("2026-09-10T00:00:00+00:00"), datetime.fromisoformat(
        "2026-09-12T00:00:00+00:00"
    )
    # the edit (10th) and import (11th) versions are the only versions in [10th, 12th)
    assert await _read(acme, lambda r: r.volume(day10, day12)) == {}


async def test_analytics_excludes_regenerate_requested(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    day4, day5 = datetime.fromisoformat("2026-09-04T00:00:00+00:00"), datetime.fromisoformat(
        "2026-09-05T00:00:00+00:00"
    )
    assert await _read(acme, lambda r: r.decisions(day4, day5)) == {}


async def test_analytics_other_tenant_rows_absent(world: AnalyticsWorld) -> None:
    """A second world's rows (two more tenants) never reach acme's reads."""
    lo, hi = _bounds()
    acme = world.tenants["acme"]
    before = await _read(acme, lambda r: r.volume(lo, hi))
    other = build_analytics_world(world.conn)
    assert await _read(acme, lambda r: r.volume(lo, hi)) == before
    assert await _read(other.tenants["bharat"], lambda r: r.volume(lo, hi)) == {"amazon": 3}
