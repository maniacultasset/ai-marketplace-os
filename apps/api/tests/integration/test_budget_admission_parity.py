"""SI10 T6/T7 (AC5; D4): one admission rule. Over the same seeded rows, `admission()` (which the API
precheck calls) and the worker's `reserve()` admit and refuse the same cases."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from psycopg import sql

from . import factories as f
from .conftest import LOCAL_HOSTS
from .seam_support import running_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
SPEND_AT = "2026-10-05T01:00:00Z"


def _credit(conn: Any, **values: Any) -> None:
    sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in values)
    conn.execute(
        sql.SQL("UPDATE ai_credit_state SET {} WHERE id = 1").format(sets), list(values.values())
    )


def _spend(conn: Any, tenant: uuid.UUID, usd: str) -> None:
    f.agent_run(conn, tenant, cost_usd=Decimal(usd), started_at=SPEND_AT)


def _global_full(conn: Any, other: uuid.UUID) -> None:
    """Another tenant (own cap raised) spends 11.99 of the platform's 12."""
    conn.execute("UPDATE tenants SET llm_daily_budget_usd = 100 WHERE id = %s", [other])
    _spend(conn, other, "11.99")


Seed = Callable[[Any, uuid.UUID, uuid.UUID], None]

CASES: dict[str, tuple[Seed, str | None]] = {
    "ok": (lambda c, a, b: None, None),
    "paused credit": (
        lambda c, a, b: _credit(c, paused_reason="credit_exhausted", paused_at=NOW),
        "credit_exhausted",
    ),
    "paused key": (
        lambda c, a, b: _credit(c, paused_reason="key_invalid", paused_at=NOW),
        "key_invalid",
    ),
    "fresh low": (
        lambda c, a, b: _credit(
            c, remaining_usd=Decimal("0.40"), checked_at=NOW - timedelta(minutes=5)
        ),
        "credit_exhausted",
    ),
    "stale low": (
        lambda c, a, b: _credit(
            c, remaining_usd=Decimal("0.40"), checked_at=NOW - timedelta(minutes=16)
        ),
        None,
    ),
    "tenant at cap - 0.01": (lambda c, a, b: _spend(c, a, "2.49"), "tenant"),
    "tenant at cap - 0.03": (lambda c, a, b: _spend(c, a, "2.47"), None),
    "global at cap": (lambda c, a, b: _global_full(c, b), "global"),
}


async def _reserve_outcome(tenant: uuid.UUID, job: uuid.UUID) -> str | None:
    from app.workers.budget import BudgetRefused, reserve

    try:
        await reserve(tenant, job, 1, now=NOW)
    except BudgetRefused as refused:
        return refused.reason if refused.scope == "credit" else refused.scope
    return None


@pytest.mark.parametrize("case", sorted(CASES))
async def test_admission_and_reserve_agree(products_world: Any, case: str) -> None:
    from app.workers.budget import admission

    seed, want = CASES[case]
    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    seed(products_world.conn, acme, bharat)
    assert await admission(acme, NOW) == want
    assert await _reserve_outcome(acme, running_job(products_world.conn, acme)) == want


async def test_admission_writes_nothing(products_world: Any) -> None:
    from app.workers.budget import admission

    acme = products_world.tenant("acme")
    job = running_job(products_world.conn, acme)
    before = products_world.conn.execute("SELECT updated_at FROM ai_credit_state").fetchone()
    assert await admission(acme, NOW) is None
    row = products_world.conn.execute(
        "SELECT cost_reserved_usd FROM job_runs WHERE id = %s", [job]
    ).fetchone()
    assert Decimal(row[0]) == 0
    assert (
        products_world.conn.execute("SELECT updated_at FROM ai_credit_state").fetchone() == before
    )


async def test_admission_other_tenant_spend_counts_only_globally(products_world: Any) -> None:
    from app.workers.budget import admission

    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    _spend(products_world.conn, bharat, "2.49")  # bharat's own cap is full, acme's is not
    assert await admission(acme, NOW) is None
    assert await admission(bharat, NOW) == "tenant"


async def test_admission_naive_now_raises(products_world: Any) -> None:
    from app.workers.budget import admission

    with pytest.raises(ValueError, match="timezone-aware"):
        await admission(products_world.tenant("acme"), NOW.replace(tzinfo=None))
