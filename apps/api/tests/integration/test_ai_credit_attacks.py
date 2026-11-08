"""SI10 tester attacks (AC2, AC5, AC6, AC15): concurrent staff Resume, concurrent pause writers,
the admission freshness edge on the real rows, a worker clock ahead of the API, a key spent to
zero, and the card and `/auth/me` agreeing with generate on a low key. `/key` is never called."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from .auth_support import PREFIX, Conn, World, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
CARD = f"{PREFIX}/admin/ai-credit"
RESUME = f"{CARD}/resume"
ME = f"{PREFIX}/auth/me"
ADMIN = "operator@acme.test"


@pytest.fixture
def world(auth_db: Conn) -> World:
    world = build_world(auth_db)
    auth_db.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [ADMIN])
    return world


def _as(world: World, email: str) -> dict[str, str]:
    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(world, email, slug))


def _set(conn: Conn, **values: Any) -> None:
    from psycopg import sql

    sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in values)
    conn.execute(
        sql.SQL("UPDATE ai_credit_state SET {} WHERE id = 1").format(sets), list(values.values())
    )


def _row(conn: Conn) -> tuple[Any, ...]:
    found = conn.execute(
        "SELECT paused_reason, paused_at, resumed_at, resumed_by_user_id FROM ai_credit_state"
    ).fetchone()
    assert found is not None
    return tuple(found)


class AlertSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, **kw: Any) -> bool:
        self.calls.append((kind, kw))
        return True


# ---- concurrency ----


async def test_concurrent_resumes_all_answer_and_record_the_admin(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    _set(auth_db, paused_reason="key_invalid", paused_at=datetime.now(UTC))
    headers = _as(world, ADMIN)
    answers = await asyncio.gather(*(api.post(RESUME, headers=headers) for _ in range(4)))
    assert [r.status_code for r in answers] == [200] * 4, [r.text for r in answers]
    assert all(r.json()["paused_reason"] is None for r in answers)
    reason, paused_at, resumed_at, resumed_by = _row(auth_db)
    assert (reason, paused_at) == (None, None)
    assert resumed_at is not None and resumed_by == world.user(ADMIN)


async def test_concurrent_pause_writers_one_wins_one_alert(
    world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import budget

    spy = AlertSpy()
    monkeypatch.setattr(budget, "alert", spy)
    at = datetime.now(UTC)
    reasons = ["credit_exhausted", "key_invalid"] * 3
    won = await asyncio.gather(*(budget.pause_ai(r, now=at) for r in reasons))  # type: ignore[arg-type]
    assert won.count(True) == 1
    winner = reasons[won.index(True)]
    assert _row(auth_db)[0] == winner
    assert [(kind, kw["key"]) for kind, kw in spy.calls] == [("llm_provider_refused", winner)]
    assert spy.calls[0][1]["level"] == "error"


# ---- admission edges on the real rows (AC5) ----


@pytest.mark.parametrize(
    ("age", "remaining", "want"),
    [
        (timedelta(minutes=15), "0.49", "credit_exhausted"),  # exactly at FRESH_FOR: still fresh
        (timedelta(minutes=15, seconds=1), "0.49", None),  # stale: fail open (decision 8)
        (timedelta(minutes=-2), "0.49", "credit_exhausted"),  # the worker's clock runs ahead
        (timedelta(0), "0.50", None),  # at the floor admits
        (timedelta(0), "0", "credit_exhausted"),  # spent to the last cent
        (timedelta(0), None, None),  # an unlimited key shows no balance
    ],
)
async def test_admission_freshness_and_floor_edges(
    world: World, auth_db: Conn, age: timedelta, remaining: str | None, want: str | None
) -> None:
    from app.workers.budget import admission

    now = datetime.now(UTC)
    _set(
        auth_db,
        checked_at=now - age,
        remaining_usd=None if remaining is None else Decimal(remaining),
        limit_usd=None if remaining is None else Decimal(10),
    )
    assert await admission(world.tenant("acme"), now) == want


async def test_low_key_reads_low_on_the_card_and_paused_on_me(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """The card, `/auth/me` and generate agree on a fresh check below the floor (no pause)."""
    _set(
        auth_db,
        checked_at=datetime.now(UTC),
        limit_usd=Decimal(10),
        usage_usd=Decimal("9.80"),
        remaining_usd=Decimal("0.20"),
    )
    card = await api.get(CARD, headers=_as(world, ADMIN))
    assert card.status_code == 200, card.text
    body = card.json()
    assert (body["state"], body["paused_reason"]) == ("low", None)
    assert body["remaining_usd"] == "0.200000"
    assert Decimal(body["floor_usd"]) == Decimal("0.50")
    me = await api.get(ME, headers=_as(world, "owner@acme.test"))
    assert me.status_code == 200, me.text
    assert me.json()["ai_paused"] is True  # the banner shows whenever generate would 503


async def test_card_and_me_never_carry_another_tenants_spend(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """The card is platform-wide by design; `/auth/me` holds no money at all."""
    me = await api.get(ME, headers=_as(world, "owner@acme.test"))
    assert me.status_code == 200, me.text
    text = me.text.lower()
    for field in ("usd", "remaining", "spend", "limit"):
        assert field not in text
