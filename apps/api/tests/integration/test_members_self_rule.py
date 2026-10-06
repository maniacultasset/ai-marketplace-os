"""S10c round-2 attacks on the self rule (T88, T114, PLAN Decision 9): a non-owner never changes
its own role, through the real app as app_rw, and the rule does not over-block."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .auth_support import (
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


def _member(conn: Conn, user: uuid.UUID) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT role, is_active, deactivated_at IS NOT NULL FROM tenant_memberships "
        "WHERE user_id = %s",
        [user],
    ).fetchone()


async def _patch(
    api: httpx.AsyncClient, token: str, target: str, body: dict[str, Any]
) -> httpx.Response:
    return await api.patch(f"{MEMBERS}/{target}", json=body, headers=bearer(token))


async def test_admin_self_role_change_with_is_active_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    admin = world.user("admin@acme.test")
    token = tokens_for(world, "admin@acme.test", "acme")
    response = await _patch(api, token, str(admin), {"role": "reviewer", "is_active": True})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _member(auth_db, admin) == ("admin", True, False)
    assert audit_rows(auth_db, "membership.role_changed") == []


async def test_admin_self_role_change_uppercase_uuid_path_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    admin = world.user("admin@acme.test")
    token = tokens_for(world, "admin@acme.test", "acme")
    response = await _patch(api, token, str(admin).upper(), {"role": "operator"})
    assert response.status_code == 403, response.text
    assert _member(auth_db, admin) == ("admin", True, False)


async def test_admin_self_role_change_with_forged_owner_claim_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """A signed access token claiming `owner` for the admin: the row's role decides."""
    admin = world.user("admin@acme.test")
    token = tokens_for(world, "admin@acme.test", "acme", role="owner")
    response = await _patch(api, token, str(admin), {"role": "reviewer"})
    assert response.status_code == 403, response.text
    assert _member(auth_db, admin) == ("admin", True, False)
    assert audit_rows(auth_db, "membership.role_changed") == []


async def test_admin_changes_other_admin_role_ok(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """The self rule is about self only: an admin still manages another non-owner's role."""
    other = add_user(auth_db, "admin2@acme.test", display_name="Admin2")
    add_member(auth_db, world.tenant("acme"), other, "admin")
    token = tokens_for(world, "admin@acme.test", "acme")
    response = await _patch(api, token, str(other), {"role": "reviewer"})
    assert response.status_code == 200, response.text
    assert _member(auth_db, other) == ("reviewer", True, False)
    assert len(audit_rows(auth_db, "membership.role_changed")) == 1


async def test_admin_self_deactivate_still_allowed(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    admin = world.user("admin@acme.test")
    token = tokens_for(world, "admin@acme.test", "acme")
    response = await _patch(api, token, str(admin), {"is_active": False})
    assert response.status_code == 200, response.text
    assert _member(auth_db, admin) == ("admin", False, True)
    # the deactivated admin's next call is refused by the membership probe
    again = await _patch(api, token, str(admin), {"is_active": True})
    assert again.status_code == 401, again.text
    assert _member(auth_db, admin) == ("admin", False, True)


async def test_owner_self_role_change_with_second_owner_ok(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    owner = world.user("owner@acme.test")
    owner_token = tokens_for(world, "owner@acme.test", "acme")
    promote = await _patch(api, owner_token, str(world.user("admin@acme.test")), {"role": "owner"})
    assert promote.status_code == 200, promote.text
    down = await _patch(api, owner_token, str(owner), {"role": "reviewer"})
    assert down.status_code == 200, down.text
    assert _member(auth_db, owner) == ("reviewer", True, False)
