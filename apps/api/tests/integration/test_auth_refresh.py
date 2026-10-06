"""S10 C9 (DW3, AC3; PLAN Decision 8): `POST /api/v1/auth/refresh` rotates within the family; a
reused token revokes the whole family, committed before the 401."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from tests.conftest import CI_JWT_SECRET

from .auth_support import PASSWORD, PREFIX, Conn, World, audit_rows, build_world, tokens_for
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

REFRESH = f"{PREFIX}/auth/refresh"


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


async def _login(api: httpx.AsyncClient, email: str) -> dict[str, Any]:
    response = await api.post(f"{PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _refresh(api: httpx.AsyncClient, token: str, **headers: str) -> httpx.Response:
    return await api.post(REFRESH, json={"refresh_token": token}, headers=headers)


def _family_rows(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT family_id, revoked_at IS NOT NULL, revoked_reason, replaced_by IS NOT NULL "
        "FROM refresh_tokens ORDER BY created_at, id"
    ).fetchall()


def _assert_401(response: httpx.Response) -> None:
    assert response.status_code == 401, response.text
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("auth_refresh", case="success")
async def test_refresh_rotates(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    first = await _login(api, "reviewer@acme.test")
    response = await _refresh(api, first["refresh_token"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refresh_token"] != first["refresh_token"]
    claims = jwt.decode(
        body["access_token"], CI_JWT_SECRET, algorithms=["HS256"], audience="ai-marketplace-os"
    )
    assert (claims["typ"], claims["roles"], claims["tid"]) == (
        "access",
        ["reviewer"],
        str(world.tenant("acme")),
    )
    assert body["memberships"][0]["tenant_id"] == str(world.tenant("acme"))
    rows = _family_rows(auth_db)
    assert len(rows) == 2 and rows[0][0] == rows[1][0]
    assert rows[0][1:] == (True, "rotated", True)
    assert rows[1][1:] == (False, None, False)
    # the new token rotates again
    assert (await _refresh(api, body["refresh_token"])).status_code == 200


async def test_refresh_reuse_revokes_chain(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    first = await _login(api, "operator@acme.test")
    second = (await _refresh(api, first["refresh_token"])).json()
    third = (await _refresh(api, second["refresh_token"])).json()
    _assert_401(await _refresh(api, first["refresh_token"]))
    # read on the admin connection after the 401: every row of the family is revoked
    rows = _family_rows(auth_db)
    assert len(rows) == 3
    assert all(revoked for _, revoked, _, _ in rows)
    assert rows[-1][2] == "reuse_detected"
    _assert_401(await _refresh(api, third["refresh_token"]))


async def test_refresh_reuse_spares_other_families(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    mine = await _login(api, "operator@acme.test")
    other = await _login(api, "operator@acme.test")
    await _refresh(api, mine["refresh_token"])
    _assert_401(await _refresh(api, mine["refresh_token"]))
    assert (await _refresh(api, other["refresh_token"])).status_code == 200


async def test_refresh_revoked_cannot_refresh(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    first = await _login(api, "owner@acme.test")
    auth_db.execute("UPDATE refresh_tokens SET revoked_at = now(), revoked_reason = 'logout'")
    _assert_401(await _refresh(api, first["refresh_token"]))


def _signed_refresh(world: World, email: str, slug: str, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(world.user(email)),
        "tid": str(world.tenant(slug)),
        "roles": [email.split("@")[0]],
        "email": email,
        "typ": "refresh",
        "jti": str(uuid.uuid4()),
        "iss": "http://localhost:8000",
        "aud": "ai-marketplace-os",
        "iat": now,
        "exp": now + 3600,
        **overrides,
    }
    return jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")


def _store(
    conn: Conn, token: str, tenant: uuid.UUID, user: uuid.UUID, *, expired: bool = False
) -> None:
    conn.execute(
        "INSERT INTO refresh_tokens (tenant_id, user_id, family_id, token_hash, expires_at) "
        "VALUES (%s, %s, gen_random_uuid(), %s, now() + %s::interval)",
        [
            tenant,
            user,
            hashlib.sha256(token.encode()).digest(),
            "-1 minute" if expired else "1 hour",
        ],
    )


async def test_refresh_expired_401(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    now = int(time.time())
    expired = _signed_refresh(world, "owner@acme.test", "acme", iat=now - 7200, exp=now - 60)
    _store(auth_db, expired, world.tenant("acme"), world.user("owner@acme.test"), expired=True)
    _assert_401(await _refresh(api, expired))
    # a live JWT whose row has expired is refused too
    stale = _signed_refresh(world, "owner@acme.test", "acme")
    _store(auth_db, stale, world.tenant("acme"), world.user("owner@acme.test"), expired=True)
    _assert_401(await _refresh(api, stale))


@pytest.mark.endpoint("auth_refresh", case="not_found")
async def test_refresh_unknown_hash_401(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    _assert_401(await _refresh(api, _signed_refresh(world, "owner@acme.test", "acme")))
    assert audit_rows(auth_db, "auth.refresh_reuse_detected") == []


async def test_refresh_tenant_mismatch_401(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    # a well-signed token naming bharat whose stored row belongs to acme
    token = _signed_refresh(world, "owner@acme.test", "bharat")
    _store(auth_db, token, world.tenant("acme"), world.user("owner@acme.test"))
    _assert_401(await _refresh(api, token))
    assert _family_rows(auth_db)[0][1] is False  # nothing was revoked or rotated


async def test_refresh_access_token_rejected(api: httpx.AsyncClient, world: World) -> None:
    body = await _login(api, "owner@acme.test")
    _assert_401(await _refresh(api, body["access_token"]))
    _assert_401(await _refresh(api, tokens_for(world, "owner@acme.test", "acme")))


async def test_refresh_deactivated_membership_401(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _login(api, "reviewer@acme.test")
    auth_db.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() WHERE user_id = %s",
        [world.user("reviewer@acme.test")],
    )
    _assert_401(await _refresh(api, body["refresh_token"]))


@pytest.mark.endpoint("auth_refresh", case="conflict")
async def test_refresh_concurrent_one_wins_other_reuse(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _login(api, "operator@acme.test")
    results = await asyncio.gather(
        _refresh(api, body["refresh_token"]), _refresh(api, body["refresh_token"])
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 401]
    winner = next(r for r in results if r.status_code == 200).json()
    # the loser saw a rotated token: reuse, so the winner's new token is dead too
    _assert_401(await _refresh(api, winner["refresh_token"]))
    assert all(revoked for _, revoked, _, _ in _family_rows(auth_db))
    # one row for the loser's reuse, one for the winner's now-revoked token
    assert len(audit_rows(auth_db, "auth.refresh_reuse_detected")) == 2


@pytest.mark.endpoint("auth_refresh", case="validation")
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"refresh_token": ""},
        {"refresh_token": "x" * 5000},
        {"refresh_token": 1},
        {"token": "x"},
    ],
)
async def test_refresh_validation_422(
    api: httpx.AsyncClient, world: World, body: dict[str, Any]
) -> None:
    response = await api.post(REFRESH, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


@pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c"])
async def test_refresh_garbage_token_401_no_write(
    api: httpx.AsyncClient, world: World, auth_db: Conn, token: str
) -> None:
    _assert_401(await _refresh(api, token))
    assert auth_db.execute("SELECT count(*) FROM audit_log").fetchone() == (0,)


async def test_auth_audit_refresh_reuse(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _login(api, "owner@acme.test")
    await _refresh(api, body["refresh_token"])
    _assert_401(await _refresh(api, body["refresh_token"], **{"X-Request-ID": "req-reuse-1"}))
    owner = world.user("owner@acme.test")
    assert audit_rows(auth_db, "auth.refresh_reuse_detected") == [
        (world.tenant("acme"), "system", None, None, "user", owner, None, None, "req-reuse-1", None)
    ]


async def test_rate_limit_refresh_61st_429(api: httpx.AsyncClient, world: World) -> None:
    for _ in range(60):
        assert (await _refresh(api, "not-a-jwt")).status_code == 401
    limited = await _refresh(api, "not-a-jwt")
    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "RATE_LIMITED"
