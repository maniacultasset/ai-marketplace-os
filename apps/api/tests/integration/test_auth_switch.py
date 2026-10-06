"""S10 C11 (AC13; PLAN Decision 4; DATABASE.md:310): `POST /api/v1/auth/switch-tenant`, as app_rw."""

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
    add_tenant,
    audit_rows,
    bearer,
    build_world,
    tokens_for,
)
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

SWITCH = f"{PREFIX}/auth/switch-tenant"
ME = f"{PREFIX}/auth/me"
USER = "operator@acme.test"


@pytest.fixture
def world(auth_db: Conn) -> World:
    world = build_world(auth_db)
    # the one user with two memberships: operator in acme, reviewer in bharat
    add_member(auth_db, world.tenant("bharat"), world.user(USER), "reviewer")
    return world


async def _access(api: httpx.AsyncClient, email: str = USER) -> str:
    response = await api.post(f"{PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def _switch(
    api: httpx.AsyncClient, token: str, target: Any, **headers: str
) -> httpx.Response:
    return await api.post(
        SWITCH, json={"target_tenant_id": str(target)}, headers={**bearer(token), **headers}
    )


def _claims(token: str) -> dict[str, Any]:
    payload: dict[str, Any] = jwt.decode(
        token, CI_JWT_SECRET, algorithms=["HS256"], audience="ai-marketplace-os"
    )
    return payload


def _assert_404(response: httpx.Response) -> None:
    assert response.status_code == 404, response.text
    assert response.json()["error"]["type"] == "NOT_FOUND"


@pytest.mark.endpoint("auth_switch_tenant", case="success")
async def test_switch_tenant_success_new_family(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _access(api)
    assert _claims(token)["tid"] == str(world.tenant("acme"))
    response = await _switch(api, token, world.tenant("bharat"))
    assert response.status_code == 200, response.text
    body = response.json()
    access, refresh = _claims(body["access_token"]), _claims(body["refresh_token"])
    assert (access["tid"], access["roles"]) == (str(world.tenant("bharat")), ["reviewer"])
    assert refresh["tid"] == str(world.tenant("bharat"))
    families = auth_db.execute(
        "SELECT tenant_id, family_id FROM refresh_tokens ORDER BY created_at"
    ).fetchall()
    assert [t for t, _ in families] == [world.tenant("acme"), world.tenant("bharat")]
    assert families[0][1] != families[1][1]
    assert {m["role"] for m in body["memberships"]} == {"operator", "reviewer"}
    me = (await api.get(ME, headers=bearer(body["access_token"]))).json()
    assert (me["tenant_id"], me["role"]) == (str(world.tenant("bharat")), "reviewer")
    # the new refresh token rotates in bharat
    rotated = await api.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": body["refresh_token"]}
    )
    assert _claims(rotated.json()["access_token"])["tid"] == str(world.tenant("bharat"))


async def test_switch_tenant_writes_under_target_guc(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _access(api)
    assert (await _switch(api, token, world.tenant("bharat"))).status_code == 200
    used = auth_db.execute(
        "SELECT tenant_id FROM tenant_memberships WHERE user_id = %s AND last_used_at IS NOT NULL "
        "ORDER BY last_used_at DESC LIMIT 1",
        [world.user(USER)],
    ).fetchone()
    assert used == (world.tenant("bharat"),)
    # the next login picks the tenant switched to
    assert _claims(await _access(api))["tid"] == str(world.tenant("bharat"))


async def test_switch_tenant_old_access_keeps_old_tid(api: httpx.AsyncClient, world: World) -> None:
    old = await _access(api)
    await _switch(api, old, world.tenant("bharat"))
    me = (await api.get(ME, headers=bearer(old))).json()
    assert (me["tenant_id"], me["role"]) == (str(world.tenant("acme")), "operator")


@pytest.mark.endpoint("auth_switch_tenant", case="wrong_tenant")
async def test_switch_tenant_non_member_404(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _access(api, "owner@acme.test")
    _assert_404(await _switch(api, token, world.tenant("bharat")))
    assert auth_db.execute("SELECT count(*) FROM refresh_tokens").fetchone() == (1,)
    assert audit_rows(auth_db, "auth.tenant_switched") == []


async def test_switch_tenant_inactive_membership_404(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _access(api)
    auth_db.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
        "WHERE tenant_id = %s AND user_id = %s",
        [world.tenant("bharat"), world.user(USER)],
    )
    _assert_404(await _switch(api, token, world.tenant("bharat")))


async def test_switch_tenant_suspended_target_404(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    paused = add_tenant(auth_db, "paused", status="suspended")
    add_member(auth_db, paused, world.user(USER), "owner")
    token = await _access(api)
    _assert_404(await _switch(api, token, paused))


@pytest.mark.endpoint("auth_switch_tenant", case="not_found")
async def test_switch_tenant_unknown_tenant_404(api: httpx.AsyncClient, world: World) -> None:
    token = await _access(api)
    _assert_404(await _switch(api, token, uuid.uuid4()))


@pytest.mark.endpoint("auth_switch_tenant", case="validation")
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"target_tenant_id": "nope"},
        {"tenant_id": str(uuid.UUID(int=1))},
        {"target_tenant_id": 1},
    ],
)
async def test_switch_tenant_validation_422(
    api: httpx.AsyncClient, world: World, body: dict[str, Any]
) -> None:
    token = await _access(api)
    response = await api.post(SWITCH, json=body, headers=bearer(token))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


@pytest.mark.endpoint("auth_switch_tenant", case="missing_auth")
async def test_switch_tenant_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    response = await api.post(SWITCH, json={"target_tenant_id": str(world.tenant("bharat"))})
    assert response.status_code == 401


def _token(world: World, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(world.user(USER)),
        "tid": str(world.tenant("acme")),
        "roles": ["operator"],
        "email": USER,
        "typ": "access",
        "jti": str(uuid.uuid4()),
        "iss": "http://localhost:8000",
        "aud": "ai-marketplace-os",
        "iat": now,
        "exp": now + 600,
        **overrides,
    }
    return jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")


@pytest.mark.endpoint("auth_switch_tenant", case="invalid_token", variant="expired")
async def test_switch_tenant_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    now = int(time.time())
    expired = _token(world, iat=now - 3600, exp=now - 1)
    assert (await _switch(api, expired, world.tenant("bharat"))).status_code == 401


@pytest.mark.endpoint("auth_switch_tenant", case="invalid_token", variant="bad_signature")
async def test_switch_tenant_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    good = _token(world)
    forged = jwt.encode(
        jwt.decode(good, options={"verify_signature": False}),
        "another_secret_that_is_at_least_32_chars",
        algorithm="HS256",
    )
    assert (await _switch(api, forged, world.tenant("bharat"))).status_code == 401


@pytest.mark.endpoint("auth_switch_tenant", case="invalid_token", variant="wrong_typ")
async def test_switch_tenant_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    refresh = tokens_for(world, USER, "acme", typ="refresh")
    assert (await _switch(api, refresh, world.tenant("bharat"))).status_code == 401


async def test_switch_tenant_same_tenant_ok(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _access(api)
    response = await _switch(api, token, world.tenant("acme"))
    assert response.status_code == 200
    assert _claims(response.json()["access_token"])["tid"] == str(world.tenant("acme"))
    families = auth_db.execute("SELECT DISTINCT family_id FROM refresh_tokens").fetchall()
    assert len(families) == 2


async def test_switch_tenant_audited(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    token = await _access(api)
    await _switch(api, token, world.tenant("bharat"), **{"X-Request-ID": "req-switch-1"})
    user = world.user(USER)
    assert audit_rows(auth_db, "auth.tenant_switched") == [
        (world.tenant("bharat"), "user", user, None, "user", user, None, None, "req-switch-1", None)
    ]
