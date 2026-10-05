"""Auth stubs (owner step S10)."""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter

from app.api.errors import NotImplementedYet
from app.api.v1.schemas.auth import LoginRequest, LogoutRequest, RefreshRequest
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


@route(router, OPS["auth_login"])
@stub
async def auth_login(body: LoginRequest) -> NoReturn:
    """Email and password in; access and refresh tokens and the user's memberships out."""
    raise NotImplementedYet("auth_login")


@route(router, OPS["auth_refresh"])
@stub
async def auth_refresh(body: RefreshRequest) -> NoReturn:
    """Rotate the refresh token; reusing a rotated one revokes the whole chain."""
    raise NotImplementedYet("auth_refresh")


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
