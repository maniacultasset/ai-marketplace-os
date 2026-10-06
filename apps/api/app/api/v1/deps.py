"""Shared `/api/v1` request dependencies (S3 T5; D9).

`bearer_required` checks only that a Bearer credential is present (S3). S10 replaces it on every
route with a policy: `require(action)` (the verified principal, then the role x action matrix,
`app/core/rbac.py`) or `public()`; the case-matrix guard fails any implemented route that still
depends on `bearer_required`.
"""

from __future__ import annotations

from functools import cache
from typing import Annotated, Any, Final

from fastapi import Depends, Header, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mkos_schemas.auth import JWTClaims
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.rate_limit import enforce_tenant_limit, limit_ip
from app.core.rbac import PUBLIC, Action, Principal, allows, mark_policy
from app.core.request_context import bind_tenant
from app.core.security import InvalidToken, decode
from app.services.auth import probe_principal

IDEMPOTENCY_KEY_PATTERN: Final = r"^[\x21-\x7E]{1,255}$"
IF_MATCH_PATTERN: Final = r'^"[0-9a-f-]{36}"$'

_bearer = HTTPBearer(auto_error=False, description="Access token (JWT) from `/api/v1/auth/login`.")


async def bearer_required(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> None:
    """Presence only: no header, an empty credential or another scheme is 401 UNAUTHENTICATED."""
    if credentials is None or not credentials.credentials.strip():
        raise ApiError(401, ErrorCode.UNAUTHENTICATED)


IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description=(
            "1-255 visible ASCII characters. The same key with the same body replays the stored "
            "response; with a different body it is 409 IDEMPOTENCY_KEY_REUSED (24 h)."
        ),
    ),
]

IfMatch = Annotated[
    str,
    Header(
        alias="If-Match",
        pattern=IF_MATCH_PATTERN,
        description=(
            'The quoted `current_version_id` the edit is based on, e.g. "<uuid>". '
            "A stale value is 409 STALE_VERSION."
        ),
    ),
]


def _unauthenticated() -> ApiError:
    return ApiError(401, ErrorCode.UNAUTHENTICATED)


async def access_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> JWTClaims:
    """The verified access-token claims (no database). Any failure is the one 401."""
    if credentials is None or not credentials.credentials.strip():
        raise _unauthenticated()
    try:
        return decode(credentials.credentials, typ="access")
    except InvalidToken:
        pass
    raise _unauthenticated()


async def limit_tenant(claims: Annotated[JWTClaims, Depends(access_claims)]) -> JWTClaims:
    """120 requests a minute per tenant, on the verified token's `tid` (no probe needed)."""
    await enforce_tenant_limit(claims.tid)
    return claims


async def get_current_principal(
    claims: Annotated[JWTClaims, Depends(limit_tenant)],
) -> Principal:
    """The verified caller (CODE-PLAN S10 C5): the access token, the tenant limiter, then one
    membership probe in its tenant; the role is the row's. Every failure is the same 401 UNAUTHENTICATED, and the
    tenant is bound to the request only after the probe passes."""
    principal = await probe_principal(claims)
    if principal is None:
        raise _unauthenticated()
    bind_tenant(principal.tenant_id)
    return principal


@cache
def require(action: Action) -> Any:
    """`Depends` on the verified principal holding `action`; 403 PERMISSION_DENIED otherwise."""

    async def dependency(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        if not allows(principal.role, action):
            raise ApiError(403, ErrorCode.PERMISSION_DENIED)
        return principal

    dependency.__name__ = f"require_{action.name.lower()}"
    return Depends(mark_policy(dependency, action))


async def _public_limited(_: Annotated[None, Depends(limit_ip)]) -> None:
    return None


async def _public_unlimited() -> None:
    return None


@cache
def public(*, rate_limited: bool = True) -> Any:
    """The public-route marker. It carries the per-IP limiter; only the probes and the docs use
    `rate_limited=False` (a liveness probe must not depend on the database)."""
    dependency = _public_limited if rate_limited else _public_unlimited
    return Depends(mark_policy(dependency, PUBLIC))
