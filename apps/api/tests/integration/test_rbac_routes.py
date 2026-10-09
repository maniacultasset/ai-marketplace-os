"""S10 C6 (DW2, AC2): every route carries a policy and a limiter, and `require()` enforces the
role x action matrix on the real app, as app_rw with real tokens."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from app.api.rate_limit import limit_ip
from app.api.v1.deps import limit_tenant
from app.api.v1.surface import OPS, Operation
from app.core.rbac import PUBLIC, route_policy
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from tests.api_requests import valid_request
from tests.support.rate_limits import reset_rate_limits

from .auth_support import ROLES, Conn, World, build_world, tokens_for
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

# Starlette routes FastAPI adds for the docs: no dependency tree, no database.
DOCS_ROUTES: Final = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})
# Public and never rate limited (a liveness probe must not depend on the database).
UNLIMITED_ROUTES: Final = frozenset({"/healthz", "/readyz"})


def _app() -> FastAPI:
    from app.main import create_app

    return create_app()


def _all_routes(app: FastAPI) -> Iterator[Any]:
    """Every route, through include_router mounts (FastAPI's route contexts)."""
    yield from iter_route_contexts(list(app.routes))


def _calls(dependant: Any) -> Iterator[Any]:
    stack = [dependant]
    while stack:
        node = stack.pop()
        yield node.call
        stack.extend(node.dependencies)


@pytest.mark.usefixtures("auth_db")
def test_every_route_declares_role() -> None:
    app = _app()
    seen: set[str] = set()
    for route in _all_routes(app):
        path = getattr(route, "path", "")
        if not hasattr(route, "dependant"):
            assert path in DOCS_ROUTES, path
            continue
        seen.add(path)
        policy = route_policy(route)
        assert policy is not None, f"{path}: no require() or public()"
        op = OPS.get(getattr(route, "operation_id", None) or "")
        if op is not None:
            assert policy == (PUBLIC if op.public else op.action), path
        else:
            assert path in UNLIMITED_ROUTES and policy == PUBLIC, path
    assert {f"/api/v1{op.path}" for op in OPS.values()} <= seen


@pytest.mark.usefixtures("auth_db")
def test_every_route_has_limiter() -> None:
    app = _app()
    for route in _all_routes(app):
        if not hasattr(route, "dependant"):
            continue
        path = getattr(route, "path", "")
        calls = set(_calls(route.dependant))
        limited = limit_ip in calls or limit_tenant in calls
        if path in UNLIMITED_ROUTES:
            assert not limited, path
        else:
            assert limited, f"{path}: no limit_ip or limit_tenant"


def test_route_walk_sees_include_router_mounts() -> None:
    # the walk is not vacuous: every registry op is found through the v1 include_router mount
    from app.main import create_app

    app = create_app()
    ids = {getattr(r, "operation_id", None) for r in _all_routes(app)}
    assert set(OPS) <= ids


async def _call(api: httpx.AsyncClient, op: Operation, token: str | None) -> httpx.Response:
    call = valid_request(op)
    headers = {**call.headers}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    kwargs: dict[str, Any] = {"headers": headers, "params": call.params}
    if call.json is not None:
        kwargs["json"] = call.json
    if call.files is not None:
        kwargs["files"] = call.files
    if call.data is not None:
        kwargs["data"] = call.data
    return await api.request(call.method, call.url, **kwargs)


NON_PUBLIC = [op for op in OPS.values() if not op.public]


@pytest.mark.parametrize("role", ROLES)
async def test_rbac_matrix(api: httpx.AsyncClient, auth_db: Conn, role: str) -> None:
    world: World = build_world(auth_db)
    token = tokens_for(world, f"{role}@acme.test", "acme")
    for op in NON_PUBLIC:
        reset_rate_limits(auth_db)
        response = await _call(api, op, token)
        if role in (op.roles or ()):
            assert response.status_code not in {401, 403}, (op.op_id, role, response.text)
            if response.status_code == 501:
                assert response.json()["error"]["type"] == "NOT_IMPLEMENTED"
        else:
            assert response.status_code == 403, (op.op_id, role, response.text)
            assert response.json()["error"]["type"] == "PERMISSION_DENIED"


async def test_reviewer_403_on_listings_generate(api: httpx.AsyncClient, auth_db: Conn) -> None:
    world = build_world(auth_db)
    response = await _call(
        api, OPS["listings_generate"], tokens_for(world, "reviewer@acme.test", "acme")
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    operator = await _call(
        api, OPS["listings_generate"], tokens_for(world, "operator@acme.test", "acme")
    )
    assert operator.status_code == 404


async def test_admin_403_on_listings_approve(api: httpx.AsyncClient, auth_db: Conn) -> None:
    world = build_world(auth_db)
    response = await _call(
        api, OPS["listings_approve"], tokens_for(world, "admin@acme.test", "acme")
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"


async def test_role_check_uses_row_not_token(api: httpx.AsyncClient, auth_db: Conn) -> None:
    # a reviewer whose token claims owner still gets 403 on an owner/operator action
    world = build_world(auth_db)
    forged_role = tokens_for(world, "reviewer@acme.test", "acme", role="owner")
    response = await _call(api, OPS["listings_generate"], forged_role)
    assert response.status_code == 403


@pytest.mark.parametrize("op", NON_PUBLIC, ids=[o.op_id for o in NON_PUBLIC])
async def test_missing_auth_401_before_any_db(api: httpx.AsyncClient, op: Operation) -> None:
    response = await _call(api, op, None)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"
