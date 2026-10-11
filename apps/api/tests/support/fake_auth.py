"""The unit suites' stand-in for the database-backed auth dependencies (CODE-PLAN S10 C6).

S3's surface tests run with no database: they need every non-public stub to answer as S3 built
it (401 without a Bearer credential, else on to the stub's 501). `install_fake_auth(app)` swaps
in a presence-only principal (an `owner`, who holds every action, so no role check refuses; it
also passes ADR-0019's platform-admin check) and a no-op per-IP limiter through FastAPI's
`dependency_overrides`. The real chain (token verification,
the tenant limiter, the membership probe and the role check) is proven against PostgreSQL in the
integration suite (`test_rbac_routes.py`, `test_api_surface_property.py`, `test_principal.py`).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from app.api.errors import ApiError
from app.api.rate_limit import limit_ip
from app.api.v1 import deps
from app.core.rbac import Principal
from fastapi import Depends, Security
from fastapi.security import HTTPAuthorizationCredentials
from mkos_schemas.codes import ErrorCode

FAKE_TENANT = uuid.UUID("0b1c2d3e-0000-4000-8000-0000000fa4e1")
FAKE_USER = uuid.UUID("0b1c2d3e-0000-4000-8000-0000000fa4e2")


async def presence_only(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(deps._bearer)],
) -> Principal:
    if credentials is None or not credentials.credentials.strip():
        raise ApiError(401, ErrorCode.UNAUTHENTICATED)
    return Principal(FAKE_USER, FAKE_TENANT, "owner", "owner@fabrica.example", "fake-jti")


async def no_limit() -> None:
    return None


async def presence_admin(
    principal: Annotated[Principal, Depends(deps.get_current_principal)],
) -> Principal:
    """ADR-0019's platform-admin check without the database: the presence-only principal."""
    return principal


def install_fake_auth(app: Any) -> Any:
    app.dependency_overrides[deps.get_current_principal] = presence_only
    app.dependency_overrides[limit_ip] = no_limit
    app.dependency_overrides[deps._platform_admin] = presence_admin
    return app
