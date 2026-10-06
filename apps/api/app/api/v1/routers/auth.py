"""Auth routes (S10): login, refresh, logout, me and the tenant switch."""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Request
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError, NotImplementedYet
from app.api.rate_limit import enforce_login_limit, request_ip
from app.api.v1.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    MembershipOut,
    RefreshRequest,
    SwitchTenantRequest,
    TokenResponse,
)
from app.api.v1.surface import OPS, route, stub
from app.services import auth

router = APIRouter()


def _tokens(pair: auth.TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        memberships=[
            MembershipOut.model_validate(
                {"tenant_id": m.tenant_id, "tenant_name": m.tenant_name, "role": m.role}
            )
            for m in pair.memberships
        ],
    )


def _unauthenticated() -> ApiError:
    return ApiError(401, ErrorCode.UNAUTHENTICATED)


@route(router, OPS["auth_login"])
async def auth_login(body: LoginRequest, request: Request) -> TokenResponse:
    """Email and password in; access and refresh tokens and the user's memberships out. The
    active tenant is the most recently used one. Every refusal is the same 401."""
    await enforce_login_limit(request, body.email)
    pair = await auth.login(
        body.email,
        body.password.get_secret_value(),
        ip=request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if pair is None:
        raise _unauthenticated()
    return _tokens(pair)


@route(router, OPS["auth_refresh"])
async def auth_refresh(body: RefreshRequest, request: Request) -> TokenResponse:
    """Rotate the refresh token; reusing a rotated one revokes the whole chain. Refreshes must
    be serialised by the client: a concurrent second refresh of one token counts as reuse."""
    pair = await auth.refresh(
        body.refresh_token.get_secret_value(),
        ip=request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if pair is None:
        raise _unauthenticated()
    return _tokens(pair)


@route(router, OPS["auth_logout"])
@stub
async def auth_logout(body: LogoutRequest) -> NoReturn:
    """Revoke the refresh token."""
    raise NotImplementedYet("auth_logout")


@route(router, OPS["auth_me"])
@stub
async def auth_me() -> NoReturn:
    """The current principal: user, active tenant, role and memberships."""
    raise NotImplementedYet("auth_me")


@route(router, OPS["auth_switch_tenant"])
@stub
async def auth_switch_tenant(body: SwitchTenantRequest) -> NoReturn:
    """A new token pair for another of the caller's active tenants."""
    raise NotImplementedYet("auth_switch_tenant")
