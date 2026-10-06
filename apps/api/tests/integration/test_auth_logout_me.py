"""S10 C10 (AC3, AC10): `POST /api/v1/auth/logout` (idempotent family revocation) and
`GET /api/v1/auth/me`, through the real app as app_rw."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from tests.conftest import CI_JWT_SECRET

from .auth_support import (
    PASSWORD,
    PREFIX,
    Conn,
    World,
    add_member,
    audit_rows,
    bearer,
    build_world,
    tokens_for,
)
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

LOGOUT = f"{PREFIX}/auth/logout"
ME = f"{PREFIX}/auth/me"


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


async def _login(api: httpx.AsyncClient, email: str) -> dict[str, Any]:
    response = await api.post(f"{PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _logout(api: httpx.AsyncClient, token: str, **headers: str) -> httpx.Response:
    return await api.post(LOGOUT, json={"refresh_token": token}, headers=headers)


def _revoked(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT revoked_at IS NOT NULL, revoked_reason FROM refresh_tokens ORDER BY created_at"
    ).fetchall()


@pytest.mark.endpoint("auth_logout", case="success")
async def test_logout_revokes_family(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    first = await _login(api, "owner@acme.test")
    second = (
        await api.post(f"{PREFIX}/auth/refresh", json={"refresh_token": first["refresh_token"]})
    ).json()
    response = await _logout(api, second["refresh_token"])
    assert response.status_code == 204
    assert response.content == b""
    assert _revoked(auth_db) == [(True, "rotated"), (True, "logout")]
    refreshed = await api.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": second["refresh_token"]}
    )
    assert refreshed.status_code == 401


async def test_logout_revokes_under_rls(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    mine = await _login(api, "operator@acme.test")
    other = await _login(api, "operator@acme.test")
    assert (await _logout(api, mine["refresh_token"])).status_code == 204
    # read on the admin connection: the logged-out family is revoked, the other one is not
    rows = auth_db.execute(
        "SELECT family_id, revoked_at IS NOT NULL FROM refresh_tokens ORDER BY created_at"
    ).fetchall()
    assert [revoked for _, revoked in rows] == [True, False]
    assert (
        await api.post(f"{PREFIX}/auth/refresh", json={"refresh_token": other["refresh_token"]})
    ).status_code == 200


@pytest.mark.endpoint("auth_logout", case="conflict")
async def test_logout_idempotent_204(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    body = await _login(api, "owner@acme.test")
    assert (await _logout(api, body["refresh_token"])).status_code == 204
    assert (await _logout(api, body["refresh_token"])).status_code == 204
    assert len(audit_rows(auth_db, "auth.logout")) == 1


def _signed(world: World, email: str, slug: str, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(world.user(email)),
        "tid": str(world.tenant(slug)),
        "roles": ["owner"],
        "email": email,
        "typ": "refresh",
        "jti": str(uuid.uuid4()),
        "iss": "http://localhost:8000",
        "aud": "ai-marketplace-os",
        "iat": now,
        "exp": now + 600,
        **overrides,
    }
    return jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")


@pytest.mark.endpoint("auth_logout", case="not_found")
async def test_logout_unknown_token_204(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    await _login(api, "owner@acme.test")
    assert (await _logout(api, _signed(world, "owner@acme.test", "acme"))).status_code == 204
    assert (await _logout(api, "not-a-jwt")).status_code == 204
    assert _revoked(auth_db) == [(False, None)]


async def test_logout_expired_token_204(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    await _login(api, "owner@acme.test")
    now = int(time.time())
    expired = _signed(world, "owner@acme.test", "acme", iat=now - 3600, exp=now - 1)
    assert (await _logout(api, expired)).status_code == 204
    assert _revoked(auth_db) == [(False, None)]
    assert audit_rows(auth_db, "auth.logout") == []


async def test_logout_access_token_is_noop(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _login(api, "owner@acme.test")
    assert (await _logout(api, body["access_token"])).status_code == 204
    assert _revoked(auth_db) == [(False, None)]


@pytest.mark.endpoint("auth_logout", case="validation")
@pytest.mark.parametrize("body", [{}, {"refresh_token": ""}, {"refresh_token": "x" * 5000}])
async def test_logout_validation_422(
    api: httpx.AsyncClient, world: World, body: dict[str, Any]
) -> None:
    response = await api.post(LOGOUT, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


async def test_auth_audit_logout(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    body = await _login(api, "reviewer@acme.test")
    await _logout(api, body["refresh_token"], **{"X-Request-ID": "req-logout-1"})
    user = world.user("reviewer@acme.test")
    assert audit_rows(auth_db, "auth.logout") == [
        (world.tenant("acme"), "user", user, None, "user", user, None, None, "req-logout-1", None)
    ]


@pytest.mark.endpoint("auth_me", case="success")
async def test_me_success(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    user = world.user("reviewer@acme.test")
    add_member(auth_db, world.tenant("bharat"), user, "operator")
    body = await _login(api, "reviewer@acme.test")
    response = await api.get(ME, headers=bearer(body["access_token"]))
    assert response.status_code == 200, response.text
    me = response.json()
    assert {k: me[k] for k in ("user_id", "email", "display_name", "tenant_id", "role")} == {
        "user_id": str(user),
        "email": "reviewer@acme.test",
        "display_name": "Reviewer",
        "tenant_id": me["tenant_id"],
        "role": "reviewer" if me["tenant_id"] == str(world.tenant("acme")) else "operator",
    }
    assert sorted((m["tenant_name"], m["role"]) for m in me["memberships"]) == [
        ("Acme", "reviewer"),
        ("Bharat", "operator"),
    ]


@pytest.mark.endpoint("auth_me", case="missing_auth")
async def test_me_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    response = await api.get(ME)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("auth_me", case="invalid_token", variant="expired")
async def test_me_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    now = int(time.time())
    expired = _signed(world, "owner@acme.test", "acme", typ="access", iat=now - 3600, exp=now - 1)
    assert (await api.get(ME, headers=bearer(expired))).status_code == 401


@pytest.mark.endpoint("auth_me", case="invalid_token", variant="bad_signature")
async def test_me_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    forged = jwt.encode(
        jwt.decode(
            tokens_for(world, "owner@acme.test", "acme"),
            options={"verify_signature": False},
        ),
        "another_secret_that_is_at_least_32_chars",
        algorithm="HS256",
    )
    assert (await api.get(ME, headers=bearer(forged))).status_code == 401


@pytest.mark.endpoint("auth_me", case="invalid_token", variant="wrong_typ")
async def test_me_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    body = await _login(api, "owner@acme.test")
    assert (await api.get(ME, headers=bearer(body["refresh_token"]))).status_code == 401


@pytest.mark.endpoint("auth_me", case="wrong_tenant")
async def test_me_token_for_other_tenant_401(api: httpx.AsyncClient, world: World) -> None:
    # a well-signed token naming a tenant the user has no membership in
    token = tokens_for(world, "owner@acme.test", "bharat", role="owner")
    assert (await api.get(ME, headers=bearer(token))).status_code == 401


async def test_me_reflects_row_role(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    body = await _login(api, "operator@acme.test")
    auth_db.execute(
        "UPDATE tenant_memberships SET role = 'reviewer' WHERE user_id = %s",
        [world.user("operator@acme.test")],
    )
    me = (await api.get(ME, headers=bearer(body["access_token"]))).json()
    assert me["role"] == "reviewer"
    assert me["memberships"] == [
        {"tenant_id": str(world.tenant("acme")), "tenant_name": "Acme", "role": "reviewer"}
    ]
