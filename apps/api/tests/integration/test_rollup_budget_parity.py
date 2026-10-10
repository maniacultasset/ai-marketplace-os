"""S23 round-2 re-check (R1-1, R1-2; PLAN AC5, AC11): the `llm_budget` alert reads the same numbers
the kill switch refuses on. For a mixed world (a tenant cap override, spend and reservations either
side of UTC midnight, a reservation on another job kind, a zeroed terminal reservation, a tenant
with reservations only) `rollup.spend_today` gives, per tenant and globally, exactly the
`spent`, `reserved`, `used` and `cap` of `BudgetRepository.tenant_state` and `global_state`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, insert_job, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
NOW = datetime(2026, 10, 9, 0, 0, 30, tzinfo=UTC)  # 30 s after UTC midnight
DEFAULT = Decimal("2.5")
PLATFORM = Decimal("12")


def _reserve(conn: Any, tenant: uuid.UUID, usd: str, at: datetime, **cols: Any) -> None:
    listing = f.listing(conn, tenant)
    values: dict[str, Any] = {
        "kind": "listing_generate",
        "entity_type": "listing",
        "entity_id": listing,
        "listing_id": listing,
        "status": "running",
        "started_at": at,
        "lease_expires_at": at + timedelta(hours=1),
        "cost_reserved_usd": Decimal(usd),
    }
    values.update(cols)
    insert_job(conn, tenant, **values)


def _world(jobs_db: JobWorld) -> tuple[uuid.UUID, uuid.UUID]:
    conn = jobs_db.conn
    acme, bharat = jobs_db.tenant("acme"), jobs_db.tenant("bharat")
    conn.execute("UPDATE tenants SET llm_daily_budget_usd = 0.75 WHERE id = %s", [acme])
    before = NOW - timedelta(seconds=40)  # yesterday 23:59:50
    # acme: today's spend, a call started yesterday and written today, a reservation from
    # yesterday, a reservation on a non-generate job, a finished job with its reservation zeroed
    f.agent_run(conn, acme, cost_usd=Decimal("0.4"), started_at=NOW, created_at=NOW)
    f.agent_run(conn, acme, cost_usd=Decimal("9"), started_at=before, created_at=NOW)
    _reserve(conn, acme, "0.10", NOW)
    _reserve(conn, acme, "0.10", before)
    insert_job(
        conn,
        acme,
        status="running",
        started_at=NOW,
        lease_expires_at=NOW + timedelta(hours=1),
        cost_reserved_usd=Decimal("5"),
    )
    _reserve(conn, acme, "0", NOW, status="succeeded", finished_at=NOW, lease_expires_at=None)
    # bharat: reservations only, default cap
    _reserve(conn, bharat, "0.10", NOW)
    _reserve(conn, bharat, "0.10", NOW)
    return acme, bharat


async def _kill_switch(tenant: uuid.UUID) -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(tenant) as uow:
        return await uow.budget.tenant_state(NOW.date(), default_cap=DEFAULT)


async def test_spend_today_matches_kill_switch(jobs_db: JobWorld) -> None:
    from app.db.session import platform_scope
    from app.repositories.budget import global_state
    from app.workers.rollup import rollup_session, spend_today

    acme, bharat = _world(jobs_db)
    async with rollup_session() as session:
        today = await spend_today(session, NOW, default_cap=DEFAULT)
    assert set(today.budgets) == {acme, bharat}
    for tenant in (acme, bharat):
        want = await _kill_switch(tenant)
        got = today.budgets[tenant]
        assert (got.spent, got.reserved, got.used, got.cap) == (
            want.spent,
            want.reserved,
            want.used,
            want.cap,
        ), tenant
    async with platform_scope() as platform:
        everyone = await global_state(platform, NOW.date(), cap=PLATFORM)
    assert (today.global_usd, today.global_reserved, today.global_used) == (
        everyone.spent,
        everyone.reserved,
        everyone.used,
    )
    # the literal numbers, so a shared mistake on both sides still fails
    assert today.budgets[acme].used == Decimal("0.500000")
    assert today.budgets[acme].cap == Decimal("0.75")
    assert today.budgets[bharat].used == Decimal("0.200000")
    assert today.budgets[bharat].cap == DEFAULT


async def test_alert_fires_where_kill_switch_refuses(jobs_db: JobWorld, log_lines: Any) -> None:
    """At 0.70 used of a 0.75 cap the kill switch refuses the next 0.10 claim; the alert has fired
    80 % (0.60) and not 100 % (0.70 < 0.75). Yesterday's 9.00 started-before-midnight call moves
    neither."""
    from app.workers.budget import BudgetRefused, reserve
    from app.workers.periodic import check_alerts

    acme, _ = _world(jobs_db)
    _reserve(jobs_db.conn, acme, "0.20", NOW)  # 0.4 spent + 0.3 reserved = 0.70
    capture = log_lines()
    with pytest.raises(BudgetRefused) as info:
        await reserve(acme, uuid.uuid4(), 1, now=NOW)
    assert info.value.scope == "tenant"
    await check_alerts(NOW)
    lines = [
        x
        for x in capture.lines()
        if x["message"] == "alert.llm_budget" and x.get("tenant_id") == str(acme)
    ]
    assert [(x["threshold_pct"], x["limit_usd"]) for x in lines] == [("80", "0.75")]


async def test_zero_cap_tenant_no_alert_no_error(jobs_db: JobWorld, log_lines: Any) -> None:
    """A tenant capped at 0 (AI switched off) spends nothing new; the alert skips it quietly."""
    from app.workers.periodic import check_alerts

    acme = jobs_db.tenant("acme")
    jobs_db.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 0 WHERE id = %s", [acme])
    f.agent_run(jobs_db.conn, acme, cost_usd=Decimal("0.3"), started_at=NOW, created_at=NOW)
    capture = log_lines()
    sent = await check_alerts(NOW)
    assert "llm_budget" not in sent
    assert not [x for x in capture.lines() if x["message"] == "alert.check_failed"]
