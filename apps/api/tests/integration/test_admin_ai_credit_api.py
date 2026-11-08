"""SI10 T13 (AC15): the platform admin's AI credit card. `GET /admin/ai-credit` shows the pause,
the last key check, the floor and today's platform spend; `POST /admin/ai-credit/resume` clears
the pause as the admin (the database guard checks it), repeatably. Everyone else is 403."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import jwt
import pytest
from tests.conftest import CI_JWT_SECRET

from . import factories as f
from .auth_support import PREFIX, Conn, World, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
CARD = f"{PREFIX}/admin/ai-credit"
RESUME = f"{CARD}/resume"
ADMIN = "operator@acme.test"
NON_ADMINS = ["owner@acme.test", "reviewer@bharat.test", "admin@acme.test"]


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


@pytest.mark.endpoint("admin_ai_credit_get", case="success")
@pytest.mark.endpoint("admin_ai_credit_get", case="wrong_tenant")
async def test_card_shows_each_state(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    """An operator of acme who is a platform admin: the card is the platform's, not a tenant's."""
    headers = _as(world, ADMIN)
    f.agent_run(
        auth_db, world.tenant("bharat"), cost_usd=Decimal("0.4"), started_at=datetime.now(UTC)
    )
    unknown = (await api.get(CARD, headers=headers)).json()
    assert unknown["state"] == "unknown"
    assert unknown["floor_usd"] == "0.500000"
    assert unknown["today_spend_usd"] == "0.400000"
    assert Decimal(unknown["daily_cap_usd"]) > 0
    assert unknown["limit_usd"] is None and unknown["checked_at"] is None
    now = datetime.now(UTC)
    _set(auth_db, limit_usd=10, usage_usd=4, remaining_usd=6, checked_at=now)
    ok = (await api.get(CARD, headers=headers)).json()
    assert (ok["state"], ok["remaining_usd"], ok["limit_usd"]) == ("ok", "6.000000", "10.000000")
    _set(auth_db, remaining_usd=Decimal("0.4"), usage_usd=Decimal("9.6"))
    assert (await api.get(CARD, headers=headers)).json()["state"] == "low"
    _set(auth_db, paused_reason="credit_exhausted", paused_at=now)
    paused = (await api.get(CARD, headers=headers)).json()
    assert (paused["state"], paused["paused_reason"]) == ("paused", "credit_exhausted")
    _set(auth_db, checked_at=now - timedelta(minutes=20), paused_reason=None, paused_at=None)
    assert (await api.get(CARD, headers=headers)).json()["state"] == "unknown"  # stale


async def test_card_holds_no_url_or_key(api: httpx.AsyncClient, world: World) -> None:
    body = (await api.get(CARD, headers=_as(world, ADMIN))).json()
    text = json.dumps(body)
    assert "http" not in text and "sk-" not in text and "openrouter" not in text.lower()


@pytest.mark.endpoint("admin_ai_credit_resume", case="success")
@pytest.mark.endpoint("admin_ai_credit_resume", case="wrong_tenant")
async def test_resume_clears_and_records_the_admin(
    api: httpx.AsyncClient, world: World, auth_db: Conn, log_lines: Any
) -> None:
    capture = log_lines()
    _set(auth_db, paused_reason="credit_exhausted", paused_at=datetime.now(UTC))
    response = await api.post(RESUME, headers=_as(world, ADMIN))
    assert response.status_code == 200, response.text
    assert response.json()["state"] != "paused"
    reason, paused_at, resumed_at, resumed_by = _row(auth_db)
    assert (reason, paused_at) == (None, None)
    assert resumed_at is not None and resumed_by == world.user(ADMIN)
    lines = [line for line in capture.lines() if line["message"] == "ai_credit.resumed"]
    assert len(lines) == 1 and lines[0]["user_id"] == str(world.user(ADMIN))
    again = await api.post(RESUME, headers=_as(world, ADMIN))
    assert again.status_code == 200
    assert again.json()["state"] == response.json()["state"]


async def test_resume_unpaused_is_200(api: httpx.AsyncClient, world: World) -> None:
    response = await api.post(RESUME, headers=_as(world, ADMIN))
    assert response.status_code == 200, response.text


async def test_after_resume_a_fresh_low_row_still_refuses_generate(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    from app.workers.budget import admission

    _set(
        auth_db,
        paused_reason="credit_exhausted",
        paused_at=datetime.now(UTC),
        remaining_usd=Decimal("0.2"),
        checked_at=datetime.now(UTC),
    )
    assert (await api.post(RESUME, headers=_as(world, ADMIN))).status_code == 200
    assert await admission(world.tenant("acme"), datetime.now(UTC)) == "credit_exhausted"


async def test_db_guard_refuses_a_non_admin_resume_behind_the_route(
    world: World, auth_db: Conn
) -> None:
    from app.core.errors import DomainError
    from app.core.rbac import Principal
    from app.services import ai_credit_admin

    owner = Principal(
        user_id=world.user("owner@acme.test"),
        tenant_id=world.tenant("acme"),
        role="owner",
        email="owner@acme.test",
        jti=str(uuid.uuid4()),
    )
    _set(auth_db, paused_reason="credit_exhausted", paused_at=datetime.now(UTC))
    with pytest.raises(DomainError):
        await ai_credit_admin.resume(owner)
    assert _row(auth_db)[0] == "credit_exhausted"


@pytest.mark.endpoint("admin_ai_credit_get", case="wrong_role")
@pytest.mark.endpoint("admin_ai_credit_resume", case="wrong_role")
@pytest.mark.parametrize("who", NON_ADMINS)
async def test_non_admin_403_row_unchanged(
    api: httpx.AsyncClient, world: World, auth_db: Conn, who: str
) -> None:
    _set(auth_db, paused_reason="credit_exhausted", paused_at=datetime.now(UTC))
    before = _row(auth_db)
    for response in (
        await api.get(CARD, headers=_as(world, who)),
        await api.post(RESUME, headers=_as(world, who)),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _row(auth_db) == before


@pytest.mark.endpoint("admin_ai_credit_get", case="missing_auth")
@pytest.mark.endpoint("admin_ai_credit_resume", case="missing_auth")
async def test_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    assert (await api.get(CARD)).status_code == 401
    assert (await api.post(RESUME)).status_code == 401


def _claims(world: World, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(world.user(ADMIN)), "tid": str(world.tenant("acme")), "roles": ["operator"],
        "email": ADMIN, "typ": "access", "jti": str(uuid.uuid4()),
        "iss": "http://localhost:8000", "aud": "ai-marketplace-os", "iat": now, "exp": now + 600,
        **overrides,
    }  # fmt: skip
    return jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")


async def _both(api: httpx.AsyncClient, headers: dict[str, str]) -> list[int]:
    return [
        (await api.get(CARD, headers=headers)).status_code,
        (await api.post(RESUME, headers=headers)).status_code,
    ]


@pytest.mark.endpoint("admin_ai_credit_get", case="invalid_token", variant="expired")
@pytest.mark.endpoint("admin_ai_credit_resume", case="invalid_token", variant="expired")
async def test_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    now = int(time.time())
    assert await _both(api, bearer(_claims(world, iat=now - 3600, exp=now - 1))) == [401, 401]


@pytest.mark.endpoint("admin_ai_credit_get", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("admin_ai_credit_resume", case="invalid_token", variant="bad_signature")
async def test_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    forged = jwt.encode(
        jwt.decode(_claims(world), options={"verify_signature": False}),
        "another_secret_that_is_at_least_32_chars",
        algorithm="HS256",
    )
    assert await _both(api, bearer(forged)) == [401, 401]


@pytest.mark.endpoint("admin_ai_credit_get", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("admin_ai_credit_resume", case="invalid_token", variant="wrong_typ")
async def test_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    assert await _both(api, bearer(_claims(world, typ="refresh"))) == [401, 401]
