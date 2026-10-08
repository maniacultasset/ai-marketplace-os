"""S18 T6 (DW8, AC 14; D10; DATABASE.md:997; ENV.md:91): the tenant budget read and the API
pre-check. App clocks are pinned (`NOW`), so no test depends on today's date."""

from __future__ import annotations

import json
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
def world(products_world: Any) -> Any:
    return products_world


async def _state(tenant: uuid.UUID) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.budget import tenant_state

    async with tenant_uow(tenant) as uow:
        return await tenant_state(uow, NOW)


async def _precheck(tenant: uuid.UUID) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.budget import precheck

    async with tenant_uow(tenant) as uow:
        await precheck(uow, NOW)


def _spend(conn: Any, tenant: uuid.UUID, cost: str, at: str = "2026-10-05T01:00:00Z") -> None:
    f.agent_run(conn, tenant, cost_usd=Decimal(cost), started_at=at)


async def test_state_sums_today_only(world: Any) -> None:
    acme = world.tenant("acme")
    _spend(world.conn, acme, "0.500000", "2026-10-04T23:59:59Z")
    _spend(world.conn, acme, "0.250000", "2026-10-05T00:00:00Z")
    _spend(world.conn, acme, "0.125000", "2026-10-06T00:00:00Z")
    state = await _state(acme)
    assert state.spent == Decimal("0.25")


async def test_cap_tenant_override(world: Any) -> None:
    acme = world.tenant("acme")
    world.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 5.00 WHERE id = %s", [acme])
    assert (await _state(acme)).cap == Decimal("5.00")


async def test_cap_env_default(world: Any) -> None:
    assert (await _state(world.tenant("acme"))).cap == Decimal("2.5")


async def test_precheck_at_cap_429(world: Any) -> None:
    from app.services.budget import BudgetExceededError

    acme = world.tenant("acme")
    _spend(world.conn, acme, "2.500000")
    with pytest.raises(BudgetExceededError) as caught:
        await _precheck(acme)
    assert caught.value.retry_after_s == 12 * 3600


async def test_precheck_below_cap_passes(world: Any) -> None:
    acme = world.tenant("acme")
    _spend(world.conn, acme, "2.499999")
    await _precheck(acme)


async def test_precheck_counts_reservations(world: Any) -> None:
    from app.services.budget import BudgetExceededError

    acme = world.tenant("acme")
    _spend(world.conn, acme, "2.000000")
    running_job(world.conn, acme, cost_reserved_usd=Decimal("0.5"))
    with pytest.raises(BudgetExceededError):
        await _precheck(acme)


async def test_state_other_tenant_spend_ignored(world: Any) -> None:
    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    _spend(world.conn, bharat, "9.000000")
    running_job(world.conn, bharat, cost_reserved_usd=Decimal("0.5"))
    state = await _state(acme)
    assert (state.spent, state.reserved) == (Decimal("0"), Decimal("0"))
    await _precheck(acme)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC), 86400),
        (datetime(2026, 10, 5, 23, 59, 59, tzinfo=UTC), 1),
        (datetime(2026, 10, 5, 23, 59, 59, 900000, tzinfo=UTC), 1),
    ],
)
def test_retry_after_seconds(now: datetime, expected: int) -> None:
    from app.services.budget import seconds_to_utc_midnight

    assert seconds_to_utc_midnight(now) == expected


def test_budget_report_has_no_amounts() -> None:
    from app.services.budget import budget_report

    body = json.dumps(budget_report().model_dump(mode="json"))
    assert "BUDGET_EXCEEDED" in body
    for figure in ("2.5", "12.0", "0.10", "USD", "$"):
        assert figure not in body
    item = budget_report().errors[0]
    assert (item.field, item.source.value) == ("listing", "system")
