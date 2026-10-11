"""Auth routes (S10): login, refresh, logout, me and the tenant switch."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.rate_limit import enforce_login_limit, request_ip
from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    MembershipOut,
    MeOut,
    RefreshRequest,
    SwitchTenantRequest,
    TokenResponse,
)
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import auth

router = APIRouter()


def _tokens(pair: auth.TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        memberships=_memberships(pair.memberships),
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
async def auth_logout(body: LogoutRequest, request: Request) -> Response:
    """Revoke the refresh token's whole family. Always 204: an invalid, expired, unknown or
    already revoked token is a no-op. An access token already issued stays valid until it
    expires (CANON.md:82)."""
    await auth.logout(body.refresh_token.get_secret_value(), ip=request_ip(request))
    return Response(status_code=204)


def _memberships(items: list[auth.Membership]) -> list[MembershipOut]:
    return [
        MembershipOut.model_validate(
            {"tenant_id": m.tenant_id, "tenant_name": m.tenant_name, "role": m.role}
        )
        for m in items
    ]


@route(router, OPS["auth_me"])
async def auth_me(principal: Annotated[Principal, Depends(get_current_principal)]) -> MeOut:
    """The current principal: user, active tenant, role (from the membership row), the live
    memberships and whether the user is a platform admin."""
    found = await auth.me(principal)
    return MeOut.model_validate(
        {
            "user_id": found.user_id,
            "email": found.email,
            "display_name": found.display_name,
            "tenant_id": found.tenant_id,
            "role": found.role,
            "memberships": _memberships(found.memberships),
            "is_platform_admin": found.is_platform_admin,
        }
    )


@route(router, OPS["auth_switch_tenant"])
async def auth_switch_tenant(
    body: SwitchTenantRequest,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> TokenResponse:
    """A new token pair, in a new family, for another of the caller's active tenants (the one
    body field naming a tenant: ADR-0006, CODE-PLAN S10 Q9). A tenant the caller is not an
    active member of, or that is not live, is 404. Tokens already issued keep their `tid`."""
    pair = await auth.switch_tenant(
        principal,
        body.target_tenant_id,
        ip=request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if pair is None:
        raise ApiError(404, ErrorCode.NOT_FOUND)
    return _tokens(pair)
