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
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.core.rbac import PUBLIC, Action, Principal, allows, mark_policy

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


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> Principal:
    """The verified caller. Every failure is the same 401 UNAUTHENTICATED."""
    raise ApiError(401, ErrorCode.UNAUTHENTICATED)


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


async def _no_limit() -> None:
    return None


@cache
def public(*, rate_limited: bool = True) -> Any:
    """The public-route marker. With `rate_limited` it carries the per-IP limiter."""

    async def dependency() -> None:
        return None

    dependency.__name__ = "public_limited" if rate_limited else "public_unlimited"
    return Depends(mark_policy(dependency, PUBLIC))
