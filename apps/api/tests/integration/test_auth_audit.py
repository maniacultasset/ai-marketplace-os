"""S10 C15 (DW8, AC8; DATABASE.md:300): every auth and membership action the API audits writes
one row with the request id, `auth.*` rows are about a `user`, and no row keeps personal data."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from .auth_support import PASSWORD, PREFIX, Conn, World, add_member, bearer, build_world
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

ACTIONS = (
    "auth.login",
    "auth.login_failed",
    "auth.refresh_reuse_detected",
    "auth.logout",
    "auth.tenant_switched",
    "membership.role_changed",
    "membership.deactivated",
)
PHONE = re.compile(r"\+?\d[\d ()-]{8,}\d")


async def _every_action(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    """One request per audited action, each with its own X-Request-ID."""

    def rid(action: str) -> dict[str, str]:
        return {"X-Request-ID": f"req-{action}"}

    login = await api.post(
        f"{PREFIX}/auth/login",
        json={"email": "operator@acme.test", "password": PASSWORD},
        headers=rid("auth.login"),
    )
    body = login.json()
    await api.post(
        f"{PREFIX}/auth/login",
        json={"email": "nobody@acme.test", "password": PASSWORD},
        headers=rid("auth.login_failed"),
    )
    rotated = await api.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": body["refresh_token"]}
    )
    await api.post(
        f"{PREFIX}/auth/refresh",
        json={"refresh_token": body["refresh_token"]},
        headers=rid("auth.refresh_reuse_detected"),
    )
    second = (
        await api.post(
            f"{PREFIX}/auth/login", json={"email": "operator@acme.test", "password": PASSWORD}
        )
    ).json()
    await api.post(
        f"{PREFIX}/auth/logout",
        json={"refresh_token": second["refresh_token"]},
        headers=rid("auth.logout"),
    )
    add_member(auth_db, world.tenant("bharat"), world.user("operator@acme.test"), "reviewer")
    await api.post(
        f"{PREFIX}/auth/switch-tenant",
        json={"target_tenant_id": str(world.tenant("bharat"))},
        headers={**bearer(second["access_token"]), **rid("auth.tenant_switched")},
    )
    owner = (
        await api.post(
            f"{PREFIX}/auth/login", json={"email": "owner@acme.test", "password": PASSWORD}
        )
    ).json()["access_token"]
    target = world.user("reviewer@acme.test")
    await api.patch(
        f"{PREFIX}/members/{target}",
        json={"role": "operator"},
        headers={**bearer(owner), **rid("membership.role_changed")},
    )
    await api.patch(
        f"{PREFIX}/members/{target}",
        json={"is_active": False},
        headers={**bearer(owner), **rid("membership.deactivated")},
    )
    assert rotated.status_code == 200


@pytest.fixture
async def audited(api: httpx.AsyncClient, auth_db: Conn) -> list[tuple[Any, ...]]:
    world = build_world(auth_db)
    await _every_action(api, world, auth_db)
    return auth_db.execute(
        "SELECT action, entity_type, request_id, before, after, reason, actor_email "
        "FROM audit_log ORDER BY occurred_at"
    ).fetchall()


async def test_auth_audit_rows_have_request_id(audited: list[tuple[Any, ...]]) -> None:
    # the request tagged for an action wrote exactly one row, of that action
    for action in ACTIONS:
        tagged = [r[0] for r in audited if r[2] == f"req-{action}"]
        assert tagged == [action], (action, tagged)
    # and every row the flows wrote carries a request id
    assert all(r[2] for r in audited)
    assert {r[0] for r in audited} == set(ACTIONS)


async def test_audit_after_has_no_personal_data(audited: list[tuple[Any, ...]]) -> None:
    assert audited
    for action, _, _, before, after, reason, actor_email in audited:
        stored = json.dumps([before, after, reason])
        assert "@" not in stored, action
        assert not PHONE.search(stored), action
        assert actor_email is None, action


async def test_auth_audit_entity_type_user(audited: list[tuple[Any, ...]]) -> None:
    auth_rows = [r for r in audited if r[0].startswith("auth.")]
    assert {r[0] for r in auth_rows} == {a for a in ACTIONS if a.startswith("auth.")}
    assert {r[1] for r in auth_rows} == {"user"}
    assert {r[1] for r in audited if r[0].startswith("membership.")} == {"membership"}
