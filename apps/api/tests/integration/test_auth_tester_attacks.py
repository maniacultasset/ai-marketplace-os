"""S10c tester attacks (PLAN AC1, AC3, AC9-AC13): cross-tenant writes through a user with two
memberships, stale-role tokens, inactive owners and the last-owner guard, concurrency on
refresh and login, and strict request bodies, all through the real app as app_rw."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
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

LOGIN = f"{PREFIX}/auth/login"
REFRESH = f"{PREFIX}/auth/refresh"
ME = f"{PREFIX}/auth/me"
SWITCH = f"{PREFIX}/auth/switch-tenant"
MEMBERS = f"{PREFIX}/members"
DUAL = "dual@both.test"


@pytest.fixture
def world(auth_db: Conn) -> World:
    w = build_world(auth_db)
    # one user with an operator membership in both tenants; acme used most recently
    w.users[DUAL] = add_user(auth_db, DUAL)
    now = datetime.now(UTC)
    add_member(auth_db, w.tenant("acme"), w.user(DUAL), "operator", last_used_at=now)
    add_member(
        auth_db, w.tenant("bharat"), w.user(DUAL), "operator", last_used_at=now - timedelta(days=1)
    )
    return w


def _as(
    world: World, email: str, slug: str | None = None, role: str | None = None
) -> dict[str, str]:
    slug = slug or email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(world, email, slug, role))


async def _login(api: httpx.AsyncClient, email: str) -> httpx.Response:
    return await api.post(LOGIN, json={"email": email, "password": PASSWORD})


def _tid(token: str) -> str:
    payload = jwt.decode(token, CI_JWT_SECRET, algorithms=["HS256"], audience="ai-marketplace-os")
    return str(payload["tid"])


def _live_tokens(conn: Conn, user: uuid.UUID, tenant: uuid.UUID) -> int:
    row = conn.execute(
        "SELECT count(*) FROM refresh_tokens WHERE user_id = %s AND tenant_id = %s "
        "AND revoked_at IS NULL",
        [user, tenant],
    ).fetchone()
    assert row is not None
    return int(row[0])


def _membership(conn: Conn, tenant: uuid.UUID, user: uuid.UUID) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT role, is_active FROM tenant_memberships WHERE tenant_id = %s AND user_id = %s",
        [tenant, user],
    ).fetchone()


# ---- cross-tenant writes through a user who belongs to both tenants ----


async def test_attack_deactivate_dual_member_touches_only_callers_tenant(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    acme, bharat, dual = world.tenant("acme"), world.tenant("bharat"), world.user(DUAL)
    first = (await _login(api, DUAL)).json()
    assert _tid(first["access_token"]) == str(acme)
    switched = await api.post(
        SWITCH, json={"target_tenant_id": str(bharat)}, headers=bearer(first["access_token"])
    )
    assert switched.status_code == 200, switched.text
    assert _live_tokens(auth_db, dual, bharat) == 1

    response = await api.patch(
        f"{MEMBERS}/{dual}", json={"is_active": False}, headers=_as(world, "owner@acme.test")
    )
    assert response.status_code == 200, response.text
    assert _membership(auth_db, acme, dual) == ("operator", False)
    assert _membership(auth_db, bharat, dual) == ("operator", True)
    assert _live_tokens(auth_db, dual, acme) == 0
    assert _live_tokens(auth_db, dual, bharat) == 1  # the other tenant's family is untouched

    # the acme access token is cut at once; the bharat one still works
    assert (await api.get(ME, headers=bearer(first["access_token"]))).status_code == 401
    me = await api.get(ME, headers=bearer(switched.json()["access_token"]))
    assert me.status_code == 200
    assert me.json()["tenant_id"] == str(bharat)
    assert [m["tenant_id"] for m in me.json()["memberships"]] == [str(bharat)]
    # a new login lands in the one live tenant left
    again = await _login(api, DUAL)
    assert again.status_code == 200
    assert _tid(again.json()["access_token"]) == str(bharat)


async def test_attack_role_change_on_dual_member_stays_in_tenant(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    dual = world.user(DUAL)
    response = await api.patch(
        f"{MEMBERS}/{dual}", json={"role": "reviewer"}, headers=_as(world, "admin@bharat.test")
    )
    assert response.status_code == 200, response.text
    assert _membership(auth_db, world.tenant("bharat"), dual) == ("reviewer", True)
    assert _membership(auth_db, world.tenant("acme"), dual) == ("operator", True)
    rows = audit_rows(auth_db, "membership.role_changed")
    assert [r[0] for r in rows] == [world.tenant("bharat")]


async def test_attack_members_list_never_shows_other_tenant_or_hashes(
    api: httpx.AsyncClient, world: World
) -> None:
    response = await api.get(MEMBERS, headers=_as(world, "owner@bharat.test"))
    assert response.status_code == 200
    emails = {m["email"] for m in response.json()["data"]}
    assert emails == {f"{r}@bharat.test" for r in ("owner", "operator", "reviewer", "admin")} | {
        DUAL
    }
    assert "argon2" not in response.text and "hashed_password" not in response.text


# ---- stale tokens: the row's role wins ----


async def test_attack_demoted_owner_old_token_cannot_manage_owners(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    second = world.users["owner2@acme.test"] = add_user(auth_db, "owner2@acme.test")
    add_member(auth_db, world.tenant("acme"), second, "owner")
    old = _as(world, "owner@acme.test")  # roles=[owner] in the token
    demote = await api.patch(
        f"{MEMBERS}/{world.user('owner@acme.test')}",
        json={"role": "admin"},
        headers=bearer(tokens_for(world, "owner2@acme.test", "acme", "owner")),
    )
    assert demote.status_code == 200, demote.text
    # the stale owner token is now an admin's: it cannot touch an owner or grant owner
    touch = await api.patch(f"{MEMBERS}/{second}", json={"role": "reviewer"}, headers=old)
    assert touch.status_code == 403
    grant = await api.patch(
        f"{MEMBERS}/{world.user('reviewer@acme.test')}", json={"role": "owner"}, headers=old
    )
    assert grant.status_code == 403
    assert _membership(auth_db, world.tenant("acme"), second) == ("owner", True)


async def test_attack_forged_role_claim_does_not_grant_team_manage(
    api: httpx.AsyncClient, world: World
) -> None:
    # a reviewer whose token claims owner (only the server can sign; the probe must still win)
    headers = _as(world, "reviewer@acme.test", role="owner")
    assert (await api.get(MEMBERS, headers=headers)).status_code == 403
    response = await api.patch(
        f"{MEMBERS}/{world.user('operator@acme.test')}", json={"role": "admin"}, headers=headers
    )
    assert response.status_code == 403


# ---- the last-owner guard ignores inactive owners ----


async def test_attack_inactive_second_owner_does_not_satisfy_guard(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    ghost = add_user(auth_db, "ghost@acme.test")
    add_member(auth_db, world.tenant("acme"), ghost, "owner", active=False)
    owner = world.user("owner@acme.test")
    headers = _as(world, "owner@acme.test")
    for body in ({"role": "admin"}, {"is_active": False}):
        response = await api.patch(f"{MEMBERS}/{owner}", json=body, headers=headers)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["type"] == "LAST_OWNER_REQUIRED"
    assert _membership(auth_db, world.tenant("acme"), owner) == ("owner", True)
    # reactivating the ghost owner first makes the demotion legal
    back = await api.patch(f"{MEMBERS}/{ghost}", json={"is_active": True}, headers=headers)
    assert back.status_code == 200
    demote = await api.patch(f"{MEMBERS}/{owner}", json={"role": "admin"}, headers=headers)
    assert demote.status_code == 200


# ---- concurrency ----


async def test_attack_five_concurrent_refreshes_one_wins_family_dead(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = (await _login(api, "operator@acme.test")).json()
    results = await asyncio.gather(
        *(api.post(REFRESH, json={"refresh_token": body["refresh_token"]}) for _ in range(5))
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 401, 401, 401, 401], [r.text for r in results]
    assert _live_tokens(auth_db, world.user("operator@acme.test"), world.tenant("acme")) == 0


async def test_attack_concurrent_logins_all_succeed_no_500(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    results = await asyncio.gather(*(_login(api, DUAL) for _ in range(5)))
    assert [r.status_code for r in results] == [200] * 5, [r.text for r in results]
    assert len({r.json()["refresh_token"] for r in results}) == 5
    assert len(audit_rows(auth_db, "auth.login")) == 5
    sixth = await _login(api, DUAL)
    assert sixth.status_code == 429


# ---- login edges ----


async def test_attack_login_email_case_insensitive(api: httpx.AsyncClient, world: World) -> None:
    response = await _login(api, "Owner@ACME.Test")
    assert response.status_code == 200, response.text
    assert _tid(response.json()["access_token"]) == str(world.tenant("acme"))


async def test_attack_login_skips_suspended_most_recent_tenant(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    auth_db.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", [world.tenant("acme")])
    response = await _login(api, DUAL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert _tid(body["access_token"]) == str(world.tenant("bharat"))
    assert [m["tenant_id"] for m in body["memberships"]] == [str(world.tenant("bharat"))]


# ---- strict bodies: no tenant can be smuggled in ----


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (SWITCH, {"target_tenant_id": None}),
        (SWITCH, {"tenant_id": "00000000-0000-0000-0000-000000000000"}),
        (
            SWITCH,
            {
                "target_tenant_id": "00000000-0000-0000-0000-000000000000",
                "tenant_id": "00000000-0000-0000-0000-000000000000",
            },
        ),
    ],
)
async def test_attack_switch_rejects_extra_or_null_tenant_fields(
    api: httpx.AsyncClient, world: World, path: str, body: dict[str, Any]
) -> None:
    response = await api.post(path, json=body, headers=_as(world, "owner@acme.test"))
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"role": "operator", "tenant_id": "00000000-0000-0000-0000-000000000000"},
        {"is_active": "false"},
        {"is_active": 0},
        {"role": "OWNER"},
        {"role": "superadmin"},
    ],
)
async def test_attack_member_patch_strict_body(
    api: httpx.AsyncClient, world: World, auth_db: Conn, body: dict[str, Any]
) -> None:
    target = world.user("operator@acme.test")
    response = await api.patch(
        f"{MEMBERS}/{target}", json=body, headers=_as(world, "owner@acme.test")
    )
    assert response.status_code == 422, response.text
    assert _membership(auth_db, world.tenant("acme"), target) == ("operator", True)


async def test_attack_member_patch_non_uuid_path_422(api: httpx.AsyncClient, world: World) -> None:
    response = await api.patch(
        f"{MEMBERS}/not-a-uuid", json={"role": "operator"}, headers=_as(world, "owner@acme.test")
    )
    assert response.status_code == 422


async def test_attack_extra_jwt_claim_rejected(api: httpx.AsyncClient, world: World) -> None:
    from app.core.security import issue_claims

    claims = issue_claims(
        sub=world.user("owner@acme.test"),
        tid=world.tenant("acme"),
        role="owner",
        email="owner@acme.test",
        typ="access",
    ).model_dump(mode="json")
    claims["tenant_id"] = str(world.tenant("bharat"))
    token = jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")
    assert (await api.get(ME, headers=bearer(token))).status_code == 401
