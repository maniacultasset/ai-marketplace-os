"""S14b T21 (AC 14; D11; KNOWN-ISSUES.md:31): the ops numbers, over a two-tenant world with fixed
timestamps, read through `platform_scope()` as `app_platform_ro`."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from . import factories as f
from .admin_support import NOW, OpsWorld
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .seam_support import Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


async def _call(fn: Any, *args: Any) -> Any:
    from app.admin.session import PlatformSessionMaker

    async with PlatformSessionMaker()() as session:
        return await fn(session, *args)


async def test_admin_ops_queue_golden(ops_world: OpsWorld, db_settings: Any) -> None:
    from app.admin.ops import QueueStat, queue_stats

    stats = await _call(queue_stats, NOW)
    assert stats == [QueueStat(ops_world.queue, depth=3, oldest_queued_seconds=600, dead_letters=3)]


async def test_admin_ops_dead_letters_no_payload_max_50(
    ops_world: OpsWorld, db_settings: Any
) -> None:
    import dataclasses

    from app.admin.ops import dead_letters

    rows = await _call(dead_letters)
    assert [r.tenant_slug for r in rows] == ["bharat"] * 3
    assert [r.finished_at for r in rows] == sorted((r.finished_at for r in rows), reverse=True)
    assert {fl.name for fl in dataclasses.fields(rows[0])} == {
        "id",
        "tenant_slug",
        "tenant_name",
        "kind",
        "error_code",
        "finished_at",
    }
    assert len(await _call(dead_letters, 2)) == 2
    for bad in (0, 51, True):
        with pytest.raises(ValueError, match="limit"):
            await _call(dead_letters, bad)


async def test_admin_ops_spend_golden_utc_boundary(ops_world: OpsWorld, db_settings: Any) -> None:
    from app.admin.ops import daily_spend

    days = await _call(daily_spend, NOW, 2)
    assert [(d.day, d.total, d.per_tenant) for d in days] == [
        (
            date(2026, 10, 4),
            Decimal("0.350000"),
            {"acme": Decimal("0.300000"), "bharat": Decimal("0.050000")},
        ),
        (date(2026, 10, 5), Decimal("0.400000"), {"bharat": Decimal("0.400000")}),
    ]
    week = await _call(daily_spend, NOW)
    assert len(week) == 7 and week[0].total == Decimal("0") and week[-1].day == date(2026, 10, 5)


async def test_admin_ops_cost_per_listing_golden(ops_world: OpsWorld, db_settings: Any) -> None:
    from app.admin.ops import cost_per_published_listing

    result = await _call(cost_per_published_listing, NOW)
    assert (result.spend, result.published, result.per_listing) == (
        Decimal("0.750000"),
        1,
        Decimal("0.750000"),
    )


async def test_admin_ops_cost_per_listing_zero_published_none(
    products_world: World, auth_db: Conn, db_settings: Any
) -> None:
    from app.admin.ops import cost_per_published_listing

    f.agent_run(auth_db, products_world.tenant("acme"), cost_usd=Decimal("1"), created_at=NOW)
    result = await _call(cost_per_published_listing, NOW)
    assert (result.published, result.per_listing) == (0, None)


async def test_admin_ops_funnel_24h_window(ops_world: OpsWorld, db_settings: Any) -> None:
    from app.admin.ops import FunnelRow, funnel

    assert await _call(funnel, NOW) == [FunnelRow("created", 2), FunnelRow("published", 1)]


async def test_admin_ops_runs_as_platform_ro(ops_world: OpsWorld, db_settings: Any) -> None:
    from sqlalchemy import text

    async def whoami(session: Any) -> Any:
        return (await session.execute(text("SELECT current_user"))).scalar()

    assert await _call(whoami) == "app_platform_ro"


def test_admin_ops_naive_now_refused() -> None:
    from app.admin.ops import _aware

    with pytest.raises(ValueError, match="aware"):
        _aware(datetime(2026, 10, 5))
