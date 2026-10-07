"""S14b T21 (AC 14; D11; KNOWN-ISSUES.md:31): the ops numbers, over a two-tenant world with fixed
timestamps, read through `platform_scope()` as `app_platform_ro`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .jobs_support import insert_job
from .seam_support import Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
REPORT = {"status": "failed", "error_code": "JOB_TIMEOUT", "errors": []}


@dataclass
class OpsWorld:
    world: World
    conn: Conn
    queue: str


def _job(conn: Conn, tenant: uuid.UUID, status: str, **columns: Any) -> uuid.UUID:
    values: dict[str, Any] = {"status": status}
    if status in ("succeeded", "failed", "dead_letter", "cancelled", "partial"):
        values.update(attempt=1, started_at=NOW - timedelta(hours=2))
        values.setdefault("finished_at", NOW - timedelta(hours=1))
    if status == "running":
        values.update(attempt=1, started_at=NOW, lease_expires_at=NOW + timedelta(minutes=5))
    if status in ("failed", "dead_letter"):
        values.update(error_report_json=Jsonb(REPORT), error_code="JOB_TIMEOUT")
    values.update(columns)
    return insert_job(conn, tenant, **values)


def _listing(
    conn: Conn,
    tenant: uuid.UUID,
    status: str,
    changed: datetime,
    first_published_at: datetime | None = None,
) -> None:
    """A listing moved straight to `status` (the status guards are skipped for the fixture row;
    the CHECKs still hold: a published row names its version and a marketplace state)."""
    listing_id = f.listing(conn, tenant)
    columns: dict[str, Any] = {"status": status, "status_changed_at": changed}
    if status == "published":
        columns.update(
            published_version_id=f.version(conn, tenant, listing_id),
            first_published_at=first_published_at,
            marketplace_state="processing",
        )
    sets = ", ".join(f"{name} = %s" for name in columns)
    conn.execute("SET session_replication_role = replica")
    try:
        conn.execute(
            f"UPDATE listings SET {sets} WHERE id = %s",  # noqa: S608 - fixed column names
            [*columns.values(), listing_id],
        )
    finally:
        conn.execute("RESET session_replication_role")


@pytest.fixture
def ops_world(products_world: World, auth_db: Conn) -> OpsWorld:
    from app.workers.policy import POLICIES
    from mkos_schemas.enums import JobKind

    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    _job(auth_db, acme, "queued", queued_at=NOW - timedelta(seconds=600))
    _job(auth_db, bharat, "queued", queued_at=NOW - timedelta(seconds=60))
    _job(auth_db, acme, "running", queued_at=NOW - timedelta(seconds=30))
    _job(auth_db, acme, "succeeded")
    for n in range(3):
        _job(auth_db, bharat, "dead_letter", finished_at=NOW - timedelta(minutes=n))
    # spend: the IST/UTC boundary and the UTC midnight
    for tenant, at, cost in (
        (acme, "2026-10-04T18:29:59Z", "0.100000"),  # 23:59:59 IST on 4 Oct
        (acme, "2026-10-04T18:30:00Z", "0.200000"),  # 00:00:00 IST on 5 Oct, still 4 Oct UTC
        (bharat, "2026-10-04T23:59:59Z", "0.050000"),
        (bharat, "2026-10-05T00:00:00Z", "0.400000"),
        (acme, "2026-09-01T00:00:00Z", "9.000000"),  # outside every window
    ):
        f.agent_run(auth_db, tenant, cost_usd=Decimal(cost), created_at=at, started_at=at)
    _listing(
        auth_db,
        acme,
        "published",
        NOW - timedelta(hours=1),
        first_published_at=NOW - timedelta(days=2),
    )
    _listing(
        auth_db,
        bharat,
        "published",
        NOW - timedelta(hours=30),
        first_published_at=NOW - timedelta(days=40),
    )
    _listing(auth_db, acme, "created", NOW - timedelta(hours=2))
    _listing(auth_db, bharat, "created", NOW - timedelta(hours=3))
    _listing(auth_db, bharat, "created", NOW - timedelta(hours=25))  # outside the 24 h window
    return OpsWorld(products_world, auth_db, POLICIES[JobKind.BULK_IMPORT].queue)


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
