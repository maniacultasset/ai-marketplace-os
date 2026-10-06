"""S10 C8 (DW1, DW8, AC1, AC12): `POST /api/v1/auth/login`, through the real app as app_rw."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from mkos_schemas.auth import JWTClaims
from tests.conftest import CI_JWT_SECRET

from .auth_support import (
    PASSWORD,
    PREFIX,
    Conn,
    World,
    add_member,
    add_tenant,
    add_user,
    audit_rows,
    build_world,
)
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

LOGIN = f"{PREFIX}/auth/login"
CLAIMS = {"sub", "tid", "roles", "email", "typ", "jti", "iss", "aud", "iat", "exp"}


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


async def _login(
    api: httpx.AsyncClient, email: str, password: str = PASSWORD, **headers: str
) -> httpx.Response:
    return await api.post(LOGIN, json={"email": email, "password": password}, headers=headers)


def _claims(token: str) -> dict[str, Any]:
    payload: dict[str, Any] = jwt.decode(
        token, CI_JWT_SECRET, algorithms=["HS256"], audience="ai-marketplace-os"
    )
    return payload


@pytest.mark.endpoint("auth_login", case="success")
async def test_login_claims(api: httpx.AsyncClient, world: World) -> None:
    response = await _login(api, "operator@acme.test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 30 * 60
    access, refresh = _claims(body["access_token"]), _claims(body["refresh_token"])
    assert set(access) == CLAIMS and set(refresh) == CLAIMS
    JWTClaims.model_validate(access)
    assert access["roles"] == ["operator"]
    assert (access["typ"], refresh["typ"]) == ("access", "refresh")
    assert access["sub"] == str(world.user("operator@acme.test"))
    assert access["tid"] == str(world.tenant("acme"))
    assert access["email"] == "operator@acme.test"
    assert access["jti"] != refresh["jti"]
    assert body["memberships"] == [
        {"tenant_id": str(world.tenant("acme")), "tenant_name": "Acme", "role": "operator"}
    ]


async def test_login_tid_latest_last_used(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    user = world.user("operator@acme.test")
    now = datetime.now(UTC)
    auth_db.execute(
        "UPDATE tenant_memberships SET last_used_at = %s WHERE user_id = %s",
        [now - timedelta(days=2), user],
    )
    add_member(auth_db, world.tenant("bharat"), user, "reviewer", last_used_at=now)
    body = (await _login(api, "OPERATOR@acme.test")).json()
    assert _claims(body["access_token"])["tid"] == str(world.tenant("bharat"))
    assert _claims(body["access_token"])["roles"] == ["reviewer"]
    assert [m["tenant_id"] for m in body["memberships"]] == [
        str(world.tenant("bharat")),
        str(world.tenant("acme")),
    ]
    # the login itself made bharat the latest
    used = auth_db.execute(
        "SELECT tenant_id FROM tenant_memberships WHERE user_id = %s "
        "ORDER BY last_used_at DESC NULLS LAST LIMIT 1",
        [user],
    ).fetchone()
    assert used == (world.tenant("bharat"),)


def _strip(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    body["error"].pop("correlation_id")
    body["error"]["error_report"].pop("request_id")
    return body


CANARY = "Canary-Pw-9z8y7x6w"
FAILURES = ("unknown_email", "wrong_password", "inactive_user", "no_active_membership", "suspended")


def _failing_login(world: World, auth_db: Conn, kind: str) -> tuple[str, str]:
    if kind == "unknown_email":
        return "nobody@acme.test", PASSWORD
    if kind == "wrong_password":
        return "owner@acme.test", "not-the-password"
    if kind == "inactive_user":
        auth_db.execute(
            "UPDATE users SET is_active = false WHERE id = %s", [world.user("admin@acme.test")]
        )
        return "admin@acme.test", PASSWORD
    if kind == "no_active_membership":
        auth_db.execute(
            "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
            "WHERE user_id = %s",
            [world.user("reviewer@acme.test")],
        )
        return "reviewer@acme.test", PASSWORD
    tenant = add_tenant(auth_db, "frozen", status="suspended")
    user = add_user(auth_db, "owner@frozen.test")
    add_member(auth_db, tenant, user, "owner")
    return "owner@frozen.test", PASSWORD


@pytest.mark.parametrize("kind", FAILURES)
async def test_login_no_user_enumeration(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from app.core.passwords import verify_password as real

    calls: list[str] = []

    def spy(plain: str, hashed: str) -> bool:
        calls.append(hashed[:9])
        return real(plain, hashed)

    monkeypatch.setattr("app.services.auth.verify_password", spy)
    baseline = await _login(api, "nobody-else@acme.test")
    assert baseline.status_code == 401
    email, password = _failing_login(world, auth_db, kind)
    calls.clear()
    response = await _login(api, email, password)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert _strip(response) == _strip(baseline)
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"
    assert len(calls) == 1 and calls[0].startswith("$argon2id")


async def test_login_wrong_password_401(api: httpx.AsyncClient, world: World) -> None:
    response = await _login(api, "owner@acme.test", CANARY)
    assert response.status_code == 401
    assert CANARY not in response.text


async def test_login_inactive_user_401(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    email, password = _failing_login(world, auth_db, "inactive_user")
    assert (await _login(api, email, password)).status_code == 401


async def test_login_no_active_membership_401(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    email, password = _failing_login(world, auth_db, "no_active_membership")
    assert (await _login(api, email, password)).status_code == 401


async def test_login_suspended_only_tenant_401(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    email, password = _failing_login(world, auth_db, "suspended")
    assert (await _login(api, email, password)).status_code == 401


@pytest.mark.endpoint("auth_login", case="validation")
@pytest.mark.parametrize(
    "body",
    [
        {"email": "", "password": CANARY},
        {"email": "owner@acme.test", "password": ""},
        {"email": "   ", "password": CANARY},
        {"email": "a" * 320 + "@acme.test", "password": CANARY},
        {"email": "owner@acme.test", "password": CANARY * 100},
        {"email": "\u043ewner@acme.test", "password": CANARY},  # a Cyrillic o
        {"email": "owner@\u0430cme.test", "password": CANARY},  # a Cyrillic a
        {"email": "owner@acme.test"},
        {"email": "owner@acme.test", "password": CANARY, "tenant_id": "x"},
        {"email": ["owner@acme.test"], "password": CANARY},
    ],
)
async def test_login_validation_422(
    api: httpx.AsyncClient, world: World, auth_db: Conn, body: dict[str, Any]
) -> None:
    response = await api.post(LOGIN, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert CANARY not in response.text
    assert audit_rows(auth_db, "auth.login_failed") == []


async def test_auth_audit_login(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    response = await _login(api, "owner@acme.test", **{"X-Request-ID": "req-login-ok"})
    assert response.status_code == 200
    rows = audit_rows(auth_db, "auth.login")
    owner = world.user("owner@acme.test")
    assert rows == [
        (world.tenant("acme"), "user", owner, None, "user", owner, None, None, "req-login-ok", None)
    ]


async def test_auth_audit_login_failed_unknown_email(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    response = await _login(api, "ghost@acme.test", **{"X-Request-ID": "req-login-unknown"})
    assert response.status_code == 401
    # read on the admin connection after the 401: the row was committed before the refusal
    assert audit_rows(auth_db, "auth.login_failed") == [
        (None, "system", None, None, "user", None, None, None, "req-login-unknown", "unknown_email")
    ]
    assert "ghost@" not in str(auth_db.execute("SELECT * FROM audit_log").fetchall())


async def test_auth_audit_login_failed_wrong_password(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    assert (await _login(api, "owner@acme.test", "bad-password")).status_code == 401
    rows = audit_rows(auth_db, "auth.login_failed")
    assert len(rows) == 1
    tenant, actor, actor_user, actor_email, entity, entity_id, *_, reason = rows[0]
    assert (tenant, actor, actor_user, actor_email, entity) == (None, "system", None, None, "user")
    assert entity_id == world.user("owner@acme.test")
    assert reason == "wrong_password"


async def test_login_writes_under_tenant_guc(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    owner = world.user("owner@acme.test")
    before = datetime.now(UTC)
    response = await _login(api, "owner@acme.test", **{"User-Agent": "test-agent/1"})
    assert response.status_code == 200
    rows = auth_db.execute(
        "SELECT tenant_id, user_id, revoked_at, user_agent, host(ip), expires_at > now() "
        "FROM refresh_tokens"
    ).fetchall()
    assert rows == [(world.tenant("acme"), owner, None, "test-agent/1", "203.0.113.7", True)]
    used = auth_db.execute(
        "SELECT m.last_used_at, u.last_login_at FROM tenant_memberships m "
        "JOIN users u ON u.id = m.user_id WHERE m.user_id = %s",
        [owner],
    ).fetchone()
    assert used is not None and used[0] >= before and used[1] >= before
