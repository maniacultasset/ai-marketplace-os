"""S10 C12 (DW9, AC9; PLAN Decision 5): `GET /api/v1/members` and `PATCH /api/v1/members/{user_id}`
through the real app as app_rw, including the owner rule and the last-owner trigger."""

from __future__ import annotations

import asyncio
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
    add_user,
    audit_rows,
    bearer,
    build_world,
    tokens_for,
)
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

MEMBERS = f"{PREFIX}/members"


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _as(world: World, email: str) -> dict[str, str]:
    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(world, email, slug))


async def _patch(
    api: httpx.AsyncClient, world: World, caller: str, target: uuid.UUID, body: dict[str, Any]
) -> httpx.Response:
    return await api.patch(f"{MEMBERS}/{target}", json=body, headers=_as(world, caller))


def _member(conn: Conn, user: uuid.UUID) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT role, is_active, deactivated_at IS NOT NULL FROM tenant_memberships "
        "WHERE user_id = %s",
        [user],
    ).fetchone()


# ---- GET /members ----


@pytest.mark.endpoint("members_list", case="success")
@pytest.mark.endpoint("members_list", case="wrong_tenant")
async def test_members_list_success(api: httpx.AsyncClient, world: World) -> None:
    response = await api.get(MEMBERS, headers=_as(world, "admin@acme.test"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert [m["email"] for m in body["data"]] == [
        f"{r}@acme.test" for r in ("owner", "operator", "reviewer", "admin")
    ]
    assert all(m["is_active"] for m in body["data"])
    assert body["page"] == {"next_cursor": None, "has_more": False, "limit": 50}
    # only acme's members: bharat's never appear
    assert not any("bharat" in m["email"] for m in body["data"])


async def test_members_list_pages(api: httpx.AsyncClient, world: World) -> None:
    first = (
        await api.get(MEMBERS, params={"limit": 3}, headers=_as(world, "owner@acme.test"))
    ).json()
    assert len(first["data"]) == 3 and first["page"]["has_more"] is True
    second = (
        await api.get(
            MEMBERS,
            params={"limit": 3, "cursor": first["page"]["next_cursor"]},
            headers=_as(world, "owner@acme.test"),
        )
    ).json()
    assert [m["email"] for m in second["data"]] == ["admin@acme.test"]
    assert second["page"]["has_more"] is False


@pytest.mark.endpoint("members_list", case="validation")
@pytest.mark.parametrize("params", [{"limit": "0"}, {"limit": "201"}, {"cursor": "!!"}])
async def test_members_list_limit_422(
    api: httpx.AsyncClient, world: World, params: dict[str, str]
) -> None:
    response = await api.get(MEMBERS, params=params, headers=_as(world, "owner@acme.test"))
    assert response.status_code == 422


@pytest.mark.endpoint("members_list", case="wrong_role")
@pytest.mark.parametrize("caller", ["operator@acme.test", "reviewer@acme.test"])
async def test_members_list_operator_403(api: httpx.AsyncClient, world: World, caller: str) -> None:
    response = await api.get(MEMBERS, headers=_as(world, caller))
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"


@pytest.mark.endpoint("members_list", case="missing_auth")
async def test_members_list_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    assert (await api.get(MEMBERS)).status_code == 401


def _token(world: World, email: str, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(world.user(email)),
        "tid": str(world.tenant("acme")),
        "roles": [email.split("@")[0]],
        "email": email,
        "typ": "access",
        "jti": str(uuid.uuid4()),
        "iss": "http://localhost:8000",
        "aud": "ai-marketplace-os",
        "iat": now,
        "exp": now + 600,
        **overrides,
    }
    return jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")


def _resigned(token: str) -> str:
    return jwt.encode(
        jwt.decode(token, options={"verify_signature": False}),
        "another_secret_that_is_at_least_32_chars",
        algorithm="HS256",
    )


@pytest.mark.endpoint("members_list", case="invalid_token", variant="expired")
@pytest.mark.endpoint("members_patch", case="invalid_token", variant="expired")
async def test_members_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    now = int(time.time())
    expired = bearer(_token(world, "owner@acme.test", iat=now - 3600, exp=now - 1))
    assert (await api.get(MEMBERS, headers=expired)).status_code == 401
    target = world.user("reviewer@acme.test")
    patched = await api.patch(f"{MEMBERS}/{target}", json={"role": "operator"}, headers=expired)
    assert patched.status_code == 401


@pytest.mark.endpoint("members_list", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("members_patch", case="invalid_token", variant="bad_signature")
async def test_members_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    forged = bearer(_resigned(_token(world, "owner@acme.test")))
    assert (await api.get(MEMBERS, headers=forged)).status_code == 401
    target = world.user("reviewer@acme.test")
    patched = await api.patch(f"{MEMBERS}/{target}", json={"role": "operator"}, headers=forged)
    assert patched.status_code == 401


@pytest.mark.endpoint("members_list", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("members_patch", case="invalid_token", variant="wrong_typ")
async def test_members_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    refresh = bearer(_token(world, "owner@acme.test", typ="refresh"))
    assert (await api.get(MEMBERS, headers=refresh)).status_code == 401
    target = world.user("reviewer@acme.test")
    patched = await api.patch(f"{MEMBERS}/{target}", json={"role": "operator"}, headers=refresh)
    assert patched.status_code == 401


# ---- PATCH /members/{user_id} ----


@pytest.mark.endpoint("members_patch", case="success")
async def test_patch_member_role_success(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    target = world.user("reviewer@acme.test")
    response = await _patch(api, world, "admin@acme.test", target, {"role": "operator"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "user_id": str(target),
        "email": "reviewer@acme.test",
        "display_name": "Reviewer",
        "role": "operator",
        "is_active": True,
    }
    assert _member(auth_db, target) == ("operator", True, False)


async def test_patch_member_deactivate_revokes_tokens(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    login = await api.post(
        f"{PREFIX}/auth/login", json={"email": "operator@acme.test", "password": PASSWORD}
    )
    refresh = login.json()["refresh_token"]
    target = world.user("operator@acme.test")
    response = await _patch(api, world, "owner@acme.test", target, {"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert _member(auth_db, target) == ("operator", False, True)
    revoked = auth_db.execute(
        "SELECT revoked_reason FROM refresh_tokens WHERE user_id = %s", [target]
    ).fetchall()
    assert revoked == [("membership_deactivated",)]
    again = await api.post(f"{PREFIX}/auth/refresh", json={"refresh_token": refresh})
    assert again.status_code == 401
    assert (
        await api.get(f"{PREFIX}/auth/me", headers=bearer(login.json()["access_token"]))
    ).status_code == 401
    # reactivated: the flag and the timestamp come back, the old tokens stay dead
    back = await _patch(api, world, "owner@acme.test", target, {"is_active": True})
    assert back.status_code == 200
    assert _member(auth_db, target) == ("operator", True, False)
    assert (
        await api.post(f"{PREFIX}/auth/refresh", json={"refresh_token": refresh})
    ).status_code == 401


@pytest.mark.endpoint("members_patch", case="wrong_role")
@pytest.mark.parametrize("body", [{"role": "reviewer"}, {"is_active": False}, {"role": "admin"}])
async def test_patch_member_admin_cannot_touch_owner_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn, body: dict[str, Any]
) -> None:
    owner = world.user("owner@acme.test")
    response = await _patch(api, world, "admin@acme.test", owner, body)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _member(auth_db, owner) == ("owner", True, False)


@pytest.mark.parametrize("target_email", ["reviewer@acme.test", "operator@acme.test"])
async def test_patch_member_admin_grants_owner_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn, target_email: str
) -> None:
    target = world.user(target_email)
    response = await _patch(api, world, "admin@acme.test", target, {"role": "owner"})
    assert response.status_code == 403
    assert _member(auth_db, target)[0] == target_email.split("@")[0]  # type: ignore[index]


async def test_patch_member_admin_self_promote_owner_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    admin = world.user("admin@acme.test")
    response = await _patch(api, world, "admin@acme.test", admin, {"role": "owner"})
    assert response.status_code == 403
    assert _member(auth_db, admin)[0] == "admin"  # type: ignore[index]


async def test_patch_member_admin_reactivates_owner_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    second = add_user(auth_db, "owner2@acme.test")
    add_member(auth_db, world.tenant("acme"), second, "owner", active=False)
    response = await _patch(api, world, "admin@acme.test", second, {"is_active": True})
    assert response.status_code == 403
    assert _member(auth_db, second) == ("owner", False, True)


async def test_patch_member_reviewer_patch_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    target = world.user("operator@acme.test")
    response = await _patch(api, world, "reviewer@acme.test", target, {"role": "reviewer"})
    assert response.status_code == 403


async def test_patch_member_owner_grants_owner_ok(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    target = world.user("admin@acme.test")
    response = await _patch(api, world, "owner@acme.test", target, {"role": "owner"})
    assert response.status_code == 200
    assert _member(auth_db, target) == ("owner", True, False)
    # now there are two owners, so the first may step down
    owner = world.user("owner@acme.test")
    down = await _patch(api, world, "owner@acme.test", owner, {"role": "operator"})
    assert down.status_code == 200


@pytest.mark.endpoint("members_patch", case="conflict")
@pytest.mark.parametrize("body", [{"role": "operator"}, {"is_active": False}])
async def test_patch_member_last_owner_409(
    api: httpx.AsyncClient, world: World, auth_db: Conn, body: dict[str, Any]
) -> None:
    owner = world.user("owner@acme.test")
    response = await _patch(api, world, "owner@acme.test", owner, body)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "LAST_OWNER_REQUIRED"
    assert _member(auth_db, owner) == ("owner", True, False)
    assert audit_rows(auth_db, "membership.role_changed") == []
    assert audit_rows(auth_db, "membership.deactivated") == []


async def test_patch_member_race_two_owners_one_409(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    second = world.user("admin@acme.test")
    auth_db.execute("UPDATE tenant_memberships SET role = 'owner' WHERE user_id = %s", [second])
    owner = world.user("owner@acme.test")
    results = await asyncio.gather(
        _patch(api, world, "owner@acme.test", second, {"role": "operator"}),
        api.patch(
            f"{MEMBERS}/{owner}",
            json={"role": "operator"},
            headers=bearer(tokens_for(world, "admin@acme.test", "acme", role="owner")),
        ),
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    owners = auth_db.execute(
        "SELECT count(*) FROM tenant_memberships WHERE tenant_id = %s AND role = 'owner' "
        "AND is_active",
        [world.tenant("acme")],
    ).fetchone()
    assert owners == (1,)


@pytest.mark.endpoint("members_patch", case="wrong_tenant")
async def test_patch_member_other_tenant_user_404(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    target = world.user("owner@bharat.test")
    response = await _patch(api, world, "owner@acme.test", target, {"role": "operator"})
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"
    assert _member(auth_db, target) == ("owner", True, False)


@pytest.mark.endpoint("members_patch", case="not_found")
async def test_patch_member_unknown_user_404(api: httpx.AsyncClient, world: World) -> None:
    response = await _patch(api, world, "owner@acme.test", uuid.uuid4(), {"role": "operator"})
    assert response.status_code == 404


@pytest.mark.endpoint("members_patch", case="validation")
async def test_patch_member_empty_body_422(api: httpx.AsyncClient, world: World) -> None:
    target = world.user("reviewer@acme.test")
    for body in ({}, {"role": None}, {"is_active": None}):
        response = await _patch(api, world, "owner@acme.test", target, body)
        assert response.status_code == 422


async def test_patch_member_bad_role_422(api: httpx.AsyncClient, world: World) -> None:
    target = world.user("reviewer@acme.test")
    for body in (
        {"role": "superuser"},
        {"is_active": "false"},
        {"role": "owner", "tenant_id": "x"},
    ):
        assert (await _patch(api, world, "owner@acme.test", target, body)).status_code == 422
    bad_id = await api.patch(
        f"{MEMBERS}/not-a-uuid", json={"role": "operator"}, headers=_as(world, "owner@acme.test")
    )
    assert bad_id.status_code == 422


@pytest.mark.endpoint("members_patch", case="missing_auth")
async def test_patch_member_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    target = world.user("reviewer@acme.test")
    assert (await api.patch(f"{MEMBERS}/{target}", json={"role": "operator"})).status_code == 401


async def test_patch_member_replay_same_body_no_second_audit(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    target = world.user("reviewer@acme.test")
    for _ in range(2):
        response = await _patch(api, world, "owner@acme.test", target, {"role": "operator"})
        assert response.status_code == 200
    assert len(audit_rows(auth_db, "membership.role_changed")) == 1


async def test_auth_audit_role_change(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    target = world.user("reviewer@acme.test")
    await api.patch(
        f"{MEMBERS}/{target}",
        json={"role": "operator", "is_active": False},
        headers={**_as(world, "owner@acme.test"), "X-Request-ID": "req-member-1"},
    )
    membership = auth_db.execute(
        "SELECT id FROM tenant_memberships WHERE user_id = %s", [target]
    ).fetchone()
    assert membership is not None
    owner = world.user("owner@acme.test")
    acme = world.tenant("acme")
    assert audit_rows(auth_db, "membership.role_changed") == [
        (
            acme,
            "user",
            owner,
            None,
            "membership",
            membership[0],
            {"role": "reviewer"},
            {"role": "operator"},
            "req-member-1",
            None,
        )
    ]
    assert audit_rows(auth_db, "membership.deactivated") == [
        (
            acme,
            "user",
            owner,
            None,
            "membership",
            membership[0],
            {"is_active": True},
            {"is_active": False},
            "req-member-1",
            None,
        )
    ]
