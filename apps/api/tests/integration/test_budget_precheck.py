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
from .generation_support import gen_world
from .seam_support import running_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
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
    _spend(world.conn, acme, "2.480000")  # SI10: + the 0.02 reservation still fits
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


# ---- SI10 T7 (AC5-AC7): the credit pause and the platform cap at the API ----

GENERATE = "/api/v1/listings/generate"
CREDIT_TEXT = "AI credit is used up. Our team has been told; nothing was changed."
KEY_TEXT = "AI is paused while our team fixes a settings problem; nothing was changed."


def _pause(conn: Any, reason: str) -> None:
    conn.execute(
        "UPDATE ai_credit_state SET paused_reason = %s, paused_at = now() WHERE id = 1", [reason]
    )


def _today_spend(conn: Any, tenant: uuid.UUID, usd: str) -> None:
    f.agent_run(conn, tenant, cost_usd=Decimal(usd), started_at=datetime.now(UTC))


def _counts(world: Any, tenant: uuid.UUID) -> tuple[int, ...]:
    tables = ("listings", "job_runs", "idempotency_keys", "audit_log")
    rows = [world.count(table, "tenant_id = %s", [tenant]) for table in tables]
    return (*rows, world.count("procrastinate_jobs"))


async def _generate(api: Any, world: Any, *, key: str | None = None, sku: str = "KURTA-002") -> Any:
    return await api.post(
        GENERATE,
        headers=world.headers("operator@acme.test", key),
        json={"product_id": str(world.product("acme", sku)), "marketplace": "amazon"},
    )


async def _regenerate(api: Any, world: Any) -> Any:
    from .generation_support import set_status

    listing_id = world.listing_of(world.product("acme", "KURTA-001"))
    assert listing_id is not None
    set_status(world, listing_id, "validated")
    return (
        await api.post(
            f"/api/v1/listings/{listing_id}/regenerate",
            headers=world.headers("operator@acme.test"),
            json={},
        ),
        listing_id,
    )


@pytest.mark.parametrize(
    ("reason", "code", "text"),
    [
        ("credit_exhausted", "AI_CREDIT_EXHAUSTED", CREDIT_TEXT),
        ("key_invalid", "AI_PROVIDER_KEY_INVALID", KEY_TEXT),
    ],
)
async def test_generate_paused_503(
    api: Any, gen_world: Any, reason: str, code: str, text: str
) -> None:
    acme = gen_world.tenant("acme")
    _pause(gen_world.conn, reason)
    before = _counts(gen_world, acme)
    response = await _generate(api, gen_world)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["type"] == code
    assert body["error"]["message"] == text
    assert "retry-after" not in response.headers
    assert "The marketplace is unavailable" not in response.text
    for figure in ("USD", "$", "0.5", "10"):
        assert figure not in body["error"]["message"]
    assert _counts(gen_world, acme) == before


@pytest.mark.parametrize(
    ("reason", "code", "text"),
    [
        ("credit_exhausted", "AI_CREDIT_EXHAUSTED", CREDIT_TEXT),
        ("key_invalid", "AI_PROVIDER_KEY_INVALID", KEY_TEXT),
    ],
)
async def test_regenerate_paused_503(
    api: Any, gen_world: Any, reason: str, code: str, text: str
) -> None:
    acme = gen_world.tenant("acme")
    _pause(gen_world.conn, reason)
    before = _counts(gen_world, acme)
    response, listing_id = await _regenerate(api, gen_world)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["type"] == code
    assert response.json()["error"]["message"] == text
    assert "retry-after" not in response.headers
    assert _counts(gen_world, acme) == before
    assert gen_world.listing(listing_id)["status"] == "validated"


async def test_generate_fresh_low_credit_503(api: Any, gen_world: Any) -> None:
    gen_world.conn.execute(
        "UPDATE ai_credit_state SET remaining_usd = 0.40, checked_at = now() WHERE id = 1"
    )
    response = await _generate(api, gen_world)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["type"] == "AI_CREDIT_EXHAUSTED"


async def test_stored_replay_after_pause_still_202(api: Any, gen_world: Any) -> None:
    first = await _generate(api, gen_world, key="replay-key-0001")
    assert first.status_code == 202, first.text
    _pause(gen_world.conn, "credit_exhausted")
    again = await _generate(api, gen_world, key="replay-key-0001")
    assert again.status_code == 202, again.text
    assert again.json() == first.json()


async def test_generate_platform_cap_429(api: Any, gen_world: Any) -> None:
    from app.services.budget import PLATFORM_BUDGET_MESSAGE

    acme, bharat = gen_world.tenant("acme"), gen_world.tenant("bharat")
    gen_world.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 100 WHERE id = %s", [bharat])
    _today_spend(gen_world.conn, bharat, "11.990000")  # + 0.02 > 12 for the platform
    before = _counts(gen_world, acme)
    response = await _generate(api, gen_world)
    assert response.status_code == 429, response.text
    body = response.json()
    assert body["error"]["type"] == "BUDGET_EXCEEDED"
    assert body["error"]["message"] == PLATFORM_BUDGET_MESSAGE
    assert 1 <= int(response.headers["retry-after"]) <= 86400
    for figure in ("11.99", "12", "USD", "$"):
        assert figure not in response.text  # no amount and no other tenant's figure
    assert _counts(gen_world, acme) == before


async def test_generate_tenant_cap_429_message(api: Any, gen_world: Any) -> None:
    from app.services.budget import BUDGET_MESSAGE

    _today_spend(gen_world.conn, gen_world.tenant("acme"), "2.490000")  # cap - 0.01
    response = await _generate(api, gen_world)
    assert response.status_code == 429, response.text
    assert response.json()["error"]["message"] == BUDGET_MESSAGE


async def test_generate_tenant_cap_minus_003_admitted(api: Any, gen_world: Any) -> None:
    _today_spend(gen_world.conn, gen_world.tenant("acme"), "2.470000")
    response = await _generate(api, gen_world)
    assert response.status_code == 202, response.text


async def test_budget_message_names_the_ist_reset(world: Any) -> None:
    """SI5 (AC19): the refusal says 5:30 am IST, and `Retry-After` still counts to 00:00 UTC."""
    from app.services.budget import BUDGET_MESSAGE, BudgetExceededError, budget_report

    assert "5:30 am IST" in BUDGET_MESSAGE and "midnight" not in BUDGET_MESSAGE
    assert budget_report().errors[0].message == BUDGET_MESSAGE
    acme = world.tenant("acme")
    _spend(world.conn, acme, "2.500000")
    with pytest.raises(BudgetExceededError) as caught:
        await _precheck(acme)
    assert caught.value.retry_after_s == 12 * 3600  # the fixed clock is 12:00 UTC
