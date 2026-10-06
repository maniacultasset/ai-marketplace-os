"""S10 C2 (DW2, AC2; PLAN Decision 9): the role x action matrix, `require(action)` and `public()`."""

from __future__ import annotations

import uuid
from typing import Annotated, get_args

import pytest
from app.api.errors import install_error_handlers
from app.api.v1.deps import get_current_principal, public, require
from app.core.rbac import PUBLIC, Action, Principal, route_policy
from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from mkos_schemas.auth import Role

pytestmark = pytest.mark.usefixtures("ci_env")

# PLAN Decision 9, written out as a literal table (owner all; every role tenant:read).
PD9: dict[str, set[str]] = {
    "owner": {
        "tenant:read",
        "product:import",
        "listing:generate",
        "listing:regenerate",
        "listing:edit",
        "listing:approve",
        "listing:publish",
        "marketplace:connect",
        "team:manage",
        "settings:manage",
        "analytics:read",
    },
    "operator": {
        "tenant:read",
        "product:import",
        "listing:generate",
        "listing:regenerate",
        "listing:edit",
        "listing:publish",
        "analytics:read",
    },
    "reviewer": {"tenant:read", "listing:approve", "listing:regenerate", "analytics:read"},
    "admin": {
        "tenant:read",
        "marketplace:connect",
        "team:manage",
        "settings:manage",
        "analytics:read",
    },
}


def test_matrix_equals_pd9() -> None:
    from app.core.rbac import MATRIX

    assert {a.value for a in Action} == PD9["owner"]
    assert len(Action) == 11
    assert set(MATRIX) == set(get_args(Role))
    assert {role: {a.value for a in actions} for role, actions in MATRIX.items()} == PD9


def test_admin_never_approves() -> None:
    # T88: admin is not an approver; only reviewer and owner hold listing:approve.
    from app.core.rbac import MATRIX, allows

    assert not allows("admin", Action.LISTING_APPROVE)
    assert not allows("operator", Action.LISTING_APPROVE)
    assert {r for r in MATRIX if allows(r, Action.LISTING_APPROVE)} == {"owner", "reviewer"}


def test_matrix_is_immutable() -> None:
    from app.core.rbac import MATRIX

    with pytest.raises(TypeError):
        MATRIX["admin"] = frozenset(Action)  # type: ignore[index]
    assert isinstance(MATRIX["admin"], frozenset)


def _principal(role: str) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=role,  # type: ignore[arg-type]
        email=f"{role}@acme.test",
        jti="jti-1",
    )


def _app(role: str) -> FastAPI:
    router = APIRouter()

    @router.post("/approve", dependencies=[require(Action.LISTING_APPROVE)])
    async def approve() -> dict[str, str]:
        return {"ok": "approve"}

    @router.get("/me")
    async def me(
        principal: Annotated[Principal, require(Action.TENANT_READ)],
    ) -> dict[str, str]:
        return {"role": principal.role}

    @router.get("/open", dependencies=[public(rate_limited=False)])
    async def open_route() -> dict[str, str]:
        return {"ok": "open"}

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: _principal(role)
    return app


@pytest.mark.parametrize("role", ["admin", "operator"])
def test_require_denies_missing_action(role: str) -> None:
    client = TestClient(_app(role), raise_server_exceptions=False)
    response = client.post("/approve")
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["type"] == "PERMISSION_DENIED"
    assert error["error_report"]["errors"][0]["source"] == "system"


@pytest.mark.parametrize("role", ["owner", "reviewer"])
def test_require_allows_held_action(role: str) -> None:
    client = TestClient(_app(role), raise_server_exceptions=False)
    assert client.post("/approve").json() == {"ok": "approve"}
    assert client.get("/me").json() == {"role": role}


def _routes(app: FastAPI) -> dict[str, object]:
    return {
        getattr(c, "path", ""): c
        for c in iter_route_contexts(list(app.routes))
        if hasattr(c, "dependant")
    }


def test_public_marker_read() -> None:
    app = _app("owner")
    policies = {path: route_policy(route) for path, route in _routes(app).items()}
    assert policies["/approve"] == Action.LISTING_APPROVE
    assert policies["/me"] == Action.TENANT_READ
    assert policies["/open"] == PUBLIC
    assert TestClient(app).get("/open").json() == {"ok": "open"}


def test_route_without_policy_reads_none() -> None:
    app = FastAPI()

    @app.get("/bare")
    async def bare() -> None:
        return None

    assert route_policy(_routes(app)["/bare"]) is None


def test_two_policies_on_one_route_refused() -> None:
    app = FastAPI()

    @app.get("/both", dependencies=[public(rate_limited=False), require(Action.TENANT_READ)])
    async def both() -> None:
        return None

    with pytest.raises(ValueError, match="more than one policy"):
        route_policy(_routes(app)["/both"])
