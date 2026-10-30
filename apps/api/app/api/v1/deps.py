"""Shared `/api/v1` request dependencies (S3 T5; D9).

`bearer_required` checks only that a Bearer credential is present (S3). S10 replaces it on every
route with a policy: `require(action)` (the verified principal, then the role x action matrix,
`app/core/rbac.py`) or `public()`; the case-matrix guard fails any implemented route that still
depends on `bearer_required`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cache
from typing import Annotated, Any, Final

from fastapi import Depends, Header, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mkos_schemas.auth import JWTClaims
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.rate_limit import enforce_refused_principal_limit, enforce_tenant_limit, limit_ip
from app.core.config import get_settings
from app.core.rbac import PLATFORM_ADMIN, PUBLIC, Action, Principal, allows, mark_policy
from app.core.request_context import bind_tenant
from app.core.security import InvalidToken, decode
from app.services import signup
from app.services.auth import has_active_factor, probe_principal
from app.services.sign_in_policy import step_up_fresh

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


async def probed_principal(claims: Annotated[JWTClaims, Depends(access_claims)]) -> Principal:
    """One membership probe in the token's tenant; the role is the row's. A verified token whose
    probe fails (a deactivated member, a suspended tenant) is counted in a bucket of its own,
    `u:<tid>:<sub>` at the tenant limit, never in the tenant's (AUTH-2, TENANCY-2), then 401."""
    principal = await probe_principal(claims)
    if principal is None:
        await enforce_refused_principal_limit(claims.tid, claims.sub)
        raise _unauthenticated()
    return principal


async def limit_tenant(principal: Annotated[Principal, Depends(probed_principal)]) -> Principal:
    """120 requests a minute per tenant, counted only for a principal the probe let through, so
    a refused token can never hold the tenant's active members at 429. The probe's connection
    is back in the pool before this autocommit increment runs."""
    await enforce_tenant_limit(principal.tenant_id)
    return principal


async def get_current_principal(
    principal: Annotated[Principal, Depends(limit_tenant)],
) -> Principal:
    """The verified caller (CODE-PLAN S10 C5): the access token, one membership probe in its
    tenant, then the tenant limiter. Every failure is the same 401 UNAUTHENTICATED, and the
    tenant is bound to the request only after the probe passes."""
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


async def _platform_admin(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """The verified caller whose own user row is a platform admin (ADR-0019), read fresh on
    every request (never from the token, so a revoked flag takes effect at once); 403
    PERMISSION_DENIED otherwise, whatever the caller's role in the token's tenant."""
    if not await signup.is_platform_admin(principal):
        raise ApiError(403, ErrorCode.PERMISSION_DENIED)
    return principal


@cache
def require_platform_admin() -> Any:
    """`Depends` on the platform-admin check, marked with the `platform:admin` policy."""
    return Depends(mark_policy(_platform_admin, PLATFORM_ADMIN))


STEP_UP_MARK: Final = "__mkos_step_up__"


@cache
def require_recent_mfa(*, otp_only: bool) -> Any:
    """DPP-1 step-up (ADR-0021): under `MFA_ENFORCEMENT=all` the access token must carry a code
    entered in the last 1800 seconds (ADR-0023; a TOTP code only, when `otp_only`); otherwise 403
    `MFA_STEP_UP_REQUIRED`, or 403 `MFA_ENROLLMENT_REQUIRED` when the caller has no factor yet
    (one read, on the refusal path only). A no-op under `off`. It runs after the op's policy,
    so a wrong role is still 403 `PERMISSION_DENIED` first."""

    async def dependency(
        claims: Annotated[JWTClaims, Depends(access_claims)],
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> None:
        if get_settings().mfa_enforcement != "all":
            return
        if step_up_fresh(claims.mfa_at, claims.amr, datetime.now(UTC), otp_only=otp_only):
            return
        if not await has_active_factor(principal.user_id):
            raise ApiError(403, ErrorCode.MFA_ENROLLMENT_REQUIRED)
        raise ApiError(403, ErrorCode.MFA_STEP_UP_REQUIRED)

    dependency.__name__ = "require_recent_mfa_otp" if otp_only else "require_recent_mfa_any"
    setattr(dependency, STEP_UP_MARK, "otp" if otp_only else "any")
    return Depends(dependency)


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
