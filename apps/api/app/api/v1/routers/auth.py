"""Auth routes (S10): login, refresh, logout, me and the tenant switch; and (DPP-1, ADR-0021) the
MFA, step-up and password routes.

Every handler turns the service's result into its status after the service's session has closed
and committed (a counted failure is never rolled back by the refusal). Under
`MFA_ENFORCEMENT=off` the MFA routes answer 501; `/auth/security` and `/auth/password` work in
both modes (the set-time password rules are live either way).
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from mkos_schemas.auth import JWTClaims
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError, password_rules_error
from app.api.rate_limit import enforce_login_limit, request_ip
from app.api.v1.deps import access_claims, get_current_principal
from app.api.v1.schemas.auth import (
    LoginConfirmOut,
    LoginConfirmRequest,
    LoginEnrollRequest,
    LoginMfaRequest,
    LoginRequest,
    LoginResult,
    LogoutRequest,
    MembershipOut,
    MeOut,
    PasswordChangeRequest,
    PasswordExpiredRequest,
    RecoveryCodesOut,
    RefreshRequest,
    SecurityOut,
    StepUpOut,
    StepUpRequest,
    SwitchTenantRequest,
    TokenResponse,
    TotpConfirmOut,
    TotpConfirmRequest,
    TotpEnrollOut,
    TotpEnrollRequest,
)
from app.api.v1.surface import OPS, route
from app.core.config import get_settings
from app.core.rbac import Principal
from app.services import auth, mfa

router = APIRouter()

MFA_OFF_MESSAGE: Final = "MFA is not enabled"
REFUSAL_MESSAGES: Final[dict[str, str]] = {
    "busy": "The request could not be completed. Try again.",
    "locked": "The request could not be completed. Try again later.",
    "wrong_password": "The current password is not correct.",
    "wrong_code": "The code is not correct.",
    "password_required": "Give the current password to set up the authenticator.",
    "too_young": "The password can be changed once a day. Try again tomorrow.",
    "reused": "The password must not be one of the last 10.",
    "not_found": "The request could not be completed.",
}

Caller = Annotated[Principal, Depends(get_current_principal)]
Claims = Annotated[JWTClaims, Depends(access_claims)]


def _tokens(pair: auth.TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        memberships=_memberships(pair.memberships),
    )


def _login_result(result: auth.SignInResult) -> LoginResult:
    if result.tokens is not None:
        pair = result.tokens
        return LoginResult(
            status="signed_in",
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            token_type="bearer",  # noqa: S106 - the OAuth token type, not a secret
            expires_in=pair.expires_in,
            memberships=_memberships(pair.memberships),
            mfa_grace_until=result.mfa_grace_until,
        )
    return LoginResult(
        status=result.status,
        challenge_token=result.challenge_token,
        challenge_expires_in=result.challenge_expires_in,
        mfa_grace_until=result.mfa_grace_until,
    )


def _unauthenticated() -> ApiError:
    return ApiError(401, ErrorCode.UNAUTHENTICATED)


def _require_mfa_on() -> None:
    """The MFA routes answer 501 under `MFA_ENFORCEMENT=off` (the dormant mode, D6)."""
    if get_settings().mfa_enforcement != "all":
        raise ApiError(501, ErrorCode.NOT_IMPLEMENTED, MFA_OFF_MESSAGE)


def _refused(refusal: auth.Refusal) -> ApiError:
    """The status of a bearer route's refusal (raised only after the service committed)."""
    if refusal.kind == "policy":
        return password_rules_error([b.message for b in refusal.breaches], field="new_password")
    if refusal.kind == "step_up_required":
        return ApiError(403, ErrorCode.MFA_STEP_UP_REQUIRED)
    if refusal.kind in ("no_session", "reauth_required"):
        return _unauthenticated()
    message = REFUSAL_MESSAGES.get(refusal.kind, REFUSAL_MESSAGES["not_found"])
    return ApiError(422, ErrorCode.VALIDATION_FAILED, message)


def _agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


@route(router, OPS["auth_login"])
async def auth_login(body: LoginRequest, request: Request) -> LoginResult:
    """Email and password in. `status` says what comes next: `signed_in` carries the access
    and refresh tokens and the user's memberships (the active tenant is the most recently used
    one); `mfa_required`, `mfa_enrollment_required` and `password_change_required` carry a
    `challenge_token` for the next step and no token. Every refusal is the same 401, a locked
    account included (10 failed passwords or codes lock it for 15 minutes)."""
    await enforce_login_limit(request, body.email)
    result = await auth.login(
        body.email,
        body.password.get_secret_value(),
        ip=request_ip(request),
        user_agent=_agent(request),
    )
    if result is None:
        raise _unauthenticated()
    return _login_result(result)


@route(router, OPS["auth_login_mfa"])
async def auth_login_mfa(body: LoginMfaRequest, request: Request) -> LoginResult:
    """The code step: the `challenge_token` of `mfa_required` and exactly one of `code` (6
    digits from the authenticator) or `recovery_code`. A wrong code, a used code, an expired or
    burned challenge (5 attempts) and a locked account are all 401."""
    _require_mfa_on()
    result = await auth.login_mfa(
        body.challenge_token.get_secret_value(),
        code=body.code,
        recovery_code=(
            body.recovery_code.get_secret_value() if body.recovery_code is not None else None
        ),
        ip=request_ip(request),
        user_agent=_agent(request),
    )
    if result is None:
        raise _unauthenticated()
    return _login_result(result)


@route(router, OPS["auth_login_mfa_enroll"])
async def auth_login_mfa_enroll(body: LoginEnrollRequest, request: Request) -> TotpEnrollOut:
    """Forced enrolment (`mfa_enrollment_required`): a new authenticator secret as an
    `otpauth://` URI (render it as a QR code) and as base32 for manual entry. The challenge
    stays valid for 15 minutes; confirm with `/auth/login/mfa/confirm`."""
    _require_mfa_on()
    enrollment = await auth.login_enroll(
        body.challenge_token.get_secret_value(), ip=request_ip(request)
    )
    if enrollment is None:
        raise _unauthenticated()
    return _enrollment(enrollment)


@route(router, OPS["auth_login_mfa_confirm"])
async def auth_login_mfa_confirm(body: LoginConfirmRequest, request: Request) -> LoginConfirmOut:
    """Forced enrolment: a code from the new authenticator. Answers the 10 recovery codes (shown
    once) and `result`, the sign-in to continue with. Every other session of the user ends."""
    _require_mfa_on()
    confirmed = await auth.login_confirm(
        body.challenge_token.get_secret_value(),
        body.code,
        ip=request_ip(request),
        user_agent=_agent(request),
    )
    if confirmed is None or confirmed.result is None:
        raise _unauthenticated()
    return LoginConfirmOut(
        recovery_codes=confirmed.recovery_codes, result=_login_result(confirmed.result)
    )


@route(router, OPS["auth_password_expired"])
async def auth_password_expired(body: PasswordExpiredRequest, request: Request) -> LoginResult:
    """The forced password change (`password_change_required`): a new password that meets the
    rules (422 names each broken rule) and is none of the last 10. Every other session ends."""
    _require_mfa_on()
    result = await auth.expired_password(
        body.challenge_token.get_secret_value(),
        body.new_password.get_secret_value(),
        ip=request_ip(request),
        user_agent=_agent(request),
    )
    if result is None:
        raise _unauthenticated()
    if isinstance(result, auth.Refusal):
        raise _refused(result)
    return _login_result(result)


def _enrollment(enrollment: mfa.Enrollment) -> TotpEnrollOut:
    return TotpEnrollOut(otpauth_uri=enrollment.otpauth_uri, secret_b32=enrollment.secret_b32)


@route(router, OPS["auth_mfa_totp_enroll"])
async def auth_mfa_totp_enroll(
    body: TotpEnrollRequest, request: Request, principal: Caller, claims: Claims
) -> TotpEnrollOut:
    """Set up the authenticator from account settings (`current_password` required while there
    is no factor), or replace it (needs a step-up in the last 10 minutes, done with a TOTP code
    or a recovery code: 403 `MFA_STEP_UP_REQUIRED` otherwise)."""
    _require_mfa_on()
    current = body.current_password.get_secret_value() if body.current_password else None
    found = await auth.totp_enroll(principal, claims, current, ip=request_ip(request))
    if isinstance(found, auth.Refusal):
        raise _refused(found)
    return _enrollment(found)


@route(router, OPS["auth_mfa_totp_confirm"])
async def auth_mfa_totp_confirm(
    body: TotpConfirmRequest, request: Request, principal: Caller, claims: Claims
) -> TotpConfirmOut:
    """Confirm the new authenticator with a code: the 10 recovery codes (shown once). The
    user's other sessions end; this one records the fresh code."""
    _require_mfa_on()
    found = await auth.totp_confirm(principal, claims, body.code, ip=request_ip(request))
    if isinstance(found, auth.Refusal):
        raise _refused(found)
    return TotpConfirmOut(recovery_codes=found)


@route(router, OPS["auth_security"])
async def auth_security(principal: Caller) -> SecurityOut:
    """The account-security card: MFA status, recovery codes left (warn at 3 or fewer), the
    enforcement mode, the grace date and the password's expiry date."""
    found = await auth.security(principal)
    return SecurityOut.model_validate(
        {
            "mfa_enrolled": found.mfa_enrolled,
            "recovery_codes_left": found.recovery_codes_left,
            "enforcement": found.enforcement,
            "mfa_grace_until": found.mfa_grace_until,
            "password_changed_at": found.password_changed_at,
            "password_expires_at": found.password_expires_at,
        }
    )


@route(router, OPS["auth_mfa_recovery_codes"])
async def auth_mfa_recovery_codes(request: Request, principal: Caller) -> RecoveryCodesOut:
    """A new set of 10 recovery codes (shown once); the old ones stop working. Needs a step-up
    in the last 10 minutes (TOTP or recovery code)."""
    _require_mfa_on()
    codes = await auth.regenerate_codes(principal, ip=request_ip(request))
    return RecoveryCodesOut(recovery_codes=codes)


@route(router, OPS["auth_mfa_step_up"])
async def auth_mfa_step_up(
    body: StepUpRequest, request: Request, principal: Caller, claims: Claims
) -> StepUpOut:
    """A code for a sensitive action: a new access token with a fresh code time (keep the stored
    refresh token). A wrong code is 422; a 401 means the session has no MFA family yet (refresh
    once, then retry)."""
    _require_mfa_on()
    found = await auth.step_up(
        principal,
        claims,
        code=body.code,
        recovery_code=(
            body.recovery_code.get_secret_value() if body.recovery_code is not None else None
        ),
        ip=request_ip(request),
    )
    if isinstance(found, auth.Refusal):
        raise _refused(found)
    return StepUpOut(access_token=found.access_token, expires_in=found.expires_in)


@route(router, OPS["auth_password_change"])
async def auth_password_change(
    body: PasswordChangeRequest, request: Request, principal: Caller, claims: Claims
) -> Response:
    """Change the password: the current one, then a new one that meets the rules (422 names
    each broken rule), is none of the last 10, at most once a day. The other sessions end."""
    refusal = await auth.change_password(
        principal,
        claims,
        body.current_password.get_secret_value(),
        body.new_password.get_secret_value(),
        ip=request_ip(request),
    )
    if refusal is not None:
        raise _refused(refusal)
    return Response(status_code=204)


@route(router, OPS["auth_refresh"])
async def auth_refresh(body: RefreshRequest, request: Request) -> TokenResponse:
    """Rotate the refresh token; reusing a rotated one revokes the whole chain. Refreshes must
    be serialised by the client: a concurrent second refresh of one token counts as reuse. With
    MFA enforced, a session whose code is 7 days old is refused: sign in again."""
    pair = await auth.refresh(
        body.refresh_token.get_secret_value(),
        ip=request_ip(request),
        user_agent=_agent(request),
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
async def auth_me(principal: Caller) -> MeOut:
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
    body: SwitchTenantRequest, request: Request, principal: Caller, claims: Claims
) -> TokenResponse:
    """A new token pair, in a new family, for another of the caller's active tenants (the one
    body field naming a tenant: ADR-0006, CODE-PLAN S10 Q9). A tenant the caller is not an
    active member of, or that is not live, is 404. Tokens already issued keep their `tid`. With
    MFA enforced, a session whose code is missing or 7 days old is 401: sign in again."""
    pair = await auth.switch_tenant(
        principal,
        body.target_tenant_id,
        claims,
        ip=request_ip(request),
        user_agent=_agent(request),
    )
    if pair is None:
        raise ApiError(404, ErrorCode.NOT_FOUND)
    if isinstance(pair, auth.Refusal):
        raise _refused(pair)
    return _tokens(pair)
