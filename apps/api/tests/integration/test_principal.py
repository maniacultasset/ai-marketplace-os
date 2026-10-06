"""S10 C5 (AC11; DATABASE.md:312): `get_current_principal` verifies the access token, then probes
the membership as app_rw; the role is the row's. Every refusal is the same 401."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
import pytest
from app.api.errors import install_error_handlers
from app.api.v1.deps import get_current_principal
from app.core.asgi import MkosApp
from app.core.rbac import Principal
from app.core.request_context import current_tenant_id
from fastapi import Depends, FastAPI

from .auth_support import Conn, World, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _probe_app() -> FastAPI:
    app = MkosApp()
    install_error_handlers(app)

    @app.get("/probe")
    async def probe(principal: Annotated[Principal, Depends(get_current_principal)]) -> Any:
        return {
            "user_id": str(principal.user_id),
            "tenant_id": str(principal.tenant_id),
            "role": principal.role,
            "email": principal.email,
            "bound_tenant": current_tenant_id(),
        }

    return app


@pytest.fixture
async def probe(auth_db: Conn) -> AsyncIterator[httpx.AsyncClient]:
    app = _probe_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _assert_401(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
    body: dict[str, Any] = response.json()
    assert body["error"]["type"] == "UNAUTHENTICATED"
    body["error"].pop("correlation_id")
    body["error"]["error_report"].pop("request_id")
    return body


async def test_principal_probe_ok_binds_tenant(probe: httpx.AsyncClient, world: World) -> None:
    token = tokens_for(world, "operator@acme.test", "acme")
    response = await probe.get("/probe", headers=bearer(token))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "user_id": str(world.user("operator@acme.test")),
        "tenant_id": str(world.tenant("acme")),
        "role": "operator",
        "email": "operator@acme.test",
        "bound_tenant": str(world.tenant("acme")),
    }


async def test_principal_probe_no_header(probe: httpx.AsyncClient, world: World) -> None:
    _assert_401(await probe.get("/probe"))


@pytest.mark.parametrize("header", ["Basic dTpw", "Bearer", "Bearer    ", "Token abc", "bearer"])
async def test_principal_probe_not_bearer(
    probe: httpx.AsyncClient, world: World, header: str
) -> None:
    _assert_401(await probe.get("/probe", headers={"Authorization": header}))


async def test_principal_probe_deactivated_membership(
    probe: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = tokens_for(world, "reviewer@acme.test", "acme")
    auth_db.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
        "WHERE user_id = %s",
        [world.user("reviewer@acme.test")],
    )
    _assert_401(await probe.get("/probe", headers=bearer(token)))


async def test_principal_probe_inactive_user(
    probe: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = tokens_for(world, "reviewer@acme.test", "acme")
    auth_db.execute(
        "UPDATE users SET is_active = false WHERE id = %s", [world.user("reviewer@acme.test")]
    )
    _assert_401(await probe.get("/probe", headers=bearer(token)))


async def test_principal_probe_suspended_tenant(
    probe: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    from .auth_support import add_member, add_tenant, add_user

    tenant = add_tenant(auth_db, "paused", status="suspended")
    user = add_user(auth_db, "owner@paused.test")
    add_member(auth_db, tenant, user, "owner")
    world.tenants["paused"], world.users["owner@paused.test"] = tenant, user
    _assert_401(
        await probe.get("/probe", headers=bearer(tokens_for(world, "owner@paused.test", "paused")))
    )


async def test_principal_probe_role_from_row(probe: httpx.AsyncClient, world: World) -> None:
    # the token says owner, the membership row says reviewer: the row wins
    token = tokens_for(world, "reviewer@acme.test", "acme", role="owner")
    response = await probe.get("/probe", headers=bearer(token))
    assert response.status_code == 200
    assert response.json()["role"] == "reviewer"


async def test_principal_probe_other_tenant_token(probe: httpx.AsyncClient, world: World) -> None:
    # a well-signed token naming a tenant the user is not a member of
    token = tokens_for(world, "owner@acme.test", "bharat", role="owner")
    _assert_401(await probe.get("/probe", headers=bearer(token)))


async def test_principal_probe_refresh_token_rejected(
    probe: httpx.AsyncClient, world: World
) -> None:
    token = tokens_for(world, "owner@acme.test", "acme", typ="refresh")
    _assert_401(await probe.get("/probe", headers=bearer(token)))


async def test_principal_probe_same_body_for_all_401(
    probe: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    good = tokens_for(world, "admin@acme.test", "acme")
    bodies = [
        _assert_401(await probe.get("/probe")),
        _assert_401(await probe.get("/probe", headers={"Authorization": "Basic x"})),
        _assert_401(await probe.get("/probe", headers=bearer("not.a.jwt"))),
        _assert_401(await probe.get("/probe", headers=bearer(good[:-2] + "xx"))),
        _assert_401(
            await probe.get(
                "/probe",
                headers=bearer(tokens_for(world, "admin@acme.test", "acme", typ="refresh")),
            )
        ),
    ]
    auth_db.execute(
        "UPDATE users SET is_active = false WHERE id = %s", [world.user("admin@acme.test")]
    )
    bodies.append(_assert_401(await probe.get("/probe", headers=bearer(good))))
    assert all(body == bodies[0] for body in bodies)
