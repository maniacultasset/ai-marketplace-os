"""Sign in and sign up with Google (ADR-0020): start, the callback, complete, the link step, unlink
and the Google sign-up. Google proves who the person is; Kuinn's own code is still asked (the
`login` of `sign_in` and `/auth/google/link` is the usual `LoginResult`).

`start` and the callback are browser navigations through the dashboard's `/api` proxy, so they
answer redirects, never JSON, for every expected outcome. The JSON steps answer 401
`OAUTH_STATE_INVALID` for an unknown, used, expired or foreign handle, the one 401
`UNAUTHENTICATED` (with the generic message) for every refusal, and 501 with the flag off. Each
rate limit runs before any handle is consumed. Every redirect is `no-store` and `no-referrer`.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.rate_limit import (
    enforce_google_limit,
    enforce_login_limit,
    enforce_signup_limit,
    request_ip,
)
from app.api.v1.deps import get_current_principal
from app.api.v1.routers.auth import _login_result, _refused
from app.api.v1.schemas.auth import LoginResult
from app.api.v1.schemas.google import (
    GoogleCompleteOut,
    GoogleCompleteRequest,
    GoogleDetailsOut,
    GoogleLinkOut,
    GoogleLinkRequest,
    GoogleSignupRequest,
)
from app.api.v1.schemas.signup import SignupStatusOut
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import auth, google_signin
from app.services.signup import SignupRow

router = APIRouter()

GENERIC_FAILURE: Final = (
    "Couldn't sign you in with Google. If you already have an account, sign in with your email "
    "and password."
)
DISABLED_MESSAGE: Final = "Google sign-in is not enabled"
FLOW_MESSAGE: Final = "This Google sign-in has expired. Start again."
UNLINK_REFUSED_MESSAGE: Final = (
    "Google is the only way into this account. Add a password before removing it."
)
QUERY_MAX: Final = 2048
REDIRECT_HEADERS: Final = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

Caller = Annotated[Principal, Depends(get_current_principal)]
OptionalParam = Annotated[str | None, Query(min_length=1, max_length=QUERY_MAX)]


def _redirect(status: int, location: str) -> Response:
    return Response(status_code=status, headers={"Location": location, **REDIRECT_HEADERS})


def _disabled() -> ApiError:
    return ApiError(501, ErrorCode.NOT_IMPLEMENTED, DISABLED_MESSAGE)


def _flow_invalid() -> ApiError:
    return ApiError(401, ErrorCode.OAUTH_STATE_INVALID, FLOW_MESSAGE)


def _generic() -> ApiError:
    return ApiError(401, ErrorCode.UNAUTHENTICATED, GENERIC_FAILURE)


def _status_out(row: SignupRow) -> SignupStatusOut:
    return SignupStatusOut.model_validate(
        {
            "request_id": row.id,
            "brand_name": row.brand_name,
            "status": row.status,
            "reason": row.reason,
            "tenant_id": row.created_tenant_id,
            "created_at": row.created_at,
            "decided_at": row.decided_at,
        }
    )


@route(router, OPS["auth_google_start"])
async def auth_google_start(
    request: Request,
    b: Annotated[str, Query(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")],
) -> Response:
    """Navigate here (same origin, through the dashboard's `/api` proxy) from both the login and
    the sign-up page. `b` is base64url(sha256(secret)) of a fresh 32-byte random secret the
    browser keeps in sessionStorage for `/auth/google/complete`. 302 to Google; with Google
    sign-in off, 302 to `/login?error=google_unavailable`."""
    await enforce_google_limit(request)
    try:
        location = await google_signin.start(b)
    except google_signin.GoogleDisabled:
        return _redirect(302, google_signin.UNAVAILABLE_LOCATION)
    except google_signin.FlowInvalid:
        raise ApiError(422, ErrorCode.VALIDATION_FAILED) from None
    return _redirect(302, location)


@route(router, OPS["auth_google_callback"])
async def auth_google_callback(
    request: Request,
    code: OptionalParam = None,
    state: OptionalParam = None,
    error: OptionalParam = None,
) -> Response:
    """Google's return (registered as the OAuth client's redirect URI). Always a 303 to the app's
    `/auth/google` route: `?handle=` (60 seconds, bound to the browser that started) or
    `?error=cancelled|failed|unavailable`. Not for direct use."""
    await enforce_google_limit(request)
    location = await google_signin.callback(
        code=code, state=state, error=error, ip=request_ip(request)
    )
    return _redirect(303, location)


@route(router, OPS["auth_google_complete"])
async def auth_google_complete(body: GoogleCompleteRequest, request: Request) -> GoogleCompleteOut:
    """The app's `/auth/google` route posts the `handle` and the browser's binding secret, then
    acts on `outcome`: `sign_in` (store tokens, or continue with `login.challenge_token` at the
    usual code, enrolment or password step), `pending` (show the request status),
    `needs_details` (the prefilled sign-up form, then `/signup/google`) or `link_required` (ask
    the existing account's password, then `/auth/google/link`). A Google sign-in always asks for
    the Kuinn code when MFA is enforced. Every refusal is the same 401 with a generic message."""
    handle = body.handle.get_secret_value()
    binding = body.binding.get_secret_value()
    await enforce_google_limit(request)
    try:
        email = await google_signin.peek_email(handle, binding, "handle")
        await enforce_login_limit(request, email)
        done = await google_signin.complete(
            handle, binding, ip=request_ip(request), user_agent=request.headers.get("user-agent")
        )
    except google_signin.GoogleDisabled:
        raise _disabled() from None
    except google_signin.FlowInvalid:
        raise _flow_invalid() from None
    if isinstance(done, google_signin.Failure):
        raise _generic()
    return _complete_out(done)


def _complete_out(done: google_signin.Completed) -> GoogleCompleteOut:
    if done.outcome == "sign_in" and done.login is not None:
        return GoogleCompleteOut(outcome="sign_in", login=_login_result(done.login))
    if done.outcome == "pending" and done.signup is not None:
        return GoogleCompleteOut(outcome="pending", signup=_status_out(done.signup))
    if done.outcome == "needs_details" and done.details is not None:
        d = done.details
        details = GoogleDetailsOut(
            email=d.email,
            owner_name=d.owner_name,
            given_name=d.given_name,
            family_name=d.family_name,
            suggested_brand_name=d.suggested_brand_name,
            details_handle=d.details_handle,
            details_expires_in=d.details_expires_in,
        )
        return GoogleCompleteOut(outcome="needs_details", details=details)
    if done.outcome == "link_required" and done.link is not None:
        link = GoogleLinkOut(
            email=done.link.email,
            link_handle=done.link.link_handle,
            link_expires_in=done.link.link_expires_in,
        )
        return GoogleCompleteOut(outcome="link_required", link=link)
    raise RuntimeError("a Google outcome without its payload")  # pragma: no cover


@route(router, OPS["auth_google_link"])
async def auth_google_link(body: GoogleLinkRequest, request: Request) -> LoginResult:
    """`link_required`: the existing account's Kuinn password. The answer is the usual login
    step (tokens, or the code, enrolment or password-change challenge); Google is linked only
    when that sign-in finishes. A wrong password counts toward the lockout and answers the same
    401 as the login; retry with the same handle until it expires (15 minutes)."""
    handle = body.link_handle.get_secret_value()
    binding = body.binding.get_secret_value()
    await enforce_google_limit(request)
    try:
        email = await google_signin.peek_email(handle, binding, "link")
        await enforce_login_limit(request, email)
        result = await google_signin.link(
            handle,
            binding,
            body.password.get_secret_value(),
            ip=request_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except google_signin.GoogleDisabled:
        raise _disabled() from None
    except google_signin.FlowInvalid:
        raise _flow_invalid() from None
    if result is None:
        raise ApiError(401, ErrorCode.UNAUTHENTICATED)
    return _login_result(result)


@route(router, OPS["auth_google_unlink"])
async def auth_google_unlink(request: Request, principal: Caller) -> Response:
    """Remove the Google account linked to the caller (needs a step-up code from the last 10
    minutes when MFA is enforced). Refused with 422 while the account has no password, since
    Google is then its only way in. 204 also when nothing is linked."""
    refusal = await auth.google_unlink(principal, ip=request_ip(request))
    if refusal is not None:
        if refusal.kind == "password_required":
            raise ApiError(422, ErrorCode.VALIDATION_FAILED, UNLINK_REFUSED_MESSAGE)
        raise _refused(refusal, masked_as="step_up_required")
    return Response(status_code=204)


@route(router, OPS["signup_google"])
async def signup_google(body: GoogleSignupRequest, request: Request) -> SignupStatusOut:
    """`needs_details`: the sign-up form after Google (brand name, owner name, optional phone and
    website; the email is Google's verified one). Always 202 with the new pending request: a
    platform admin approves it as for any sign-up (it shows "Verified by Google"). After
    approval, "Continue with Google" signs in (the authenticator is set up at the first sign-in).
    The generic 401 when the email or Google account got an account meanwhile."""
    handle = body.details_handle.get_secret_value()
    binding = body.binding.get_secret_value()
    await enforce_google_limit(request)
    await enforce_signup_limit(request)
    try:
        email = await google_signin.peek_email(handle, binding, "details")
        await enforce_login_limit(request, email)
        created = await google_signin.signup_details(
            handle,
            binding,
            brand_name=body.brand_name,
            owner_name=body.owner_name,
            phone=body.phone,
            website=body.website,
            ip=request_ip(request),
        )
    except google_signin.GoogleDisabled:
        raise _disabled() from None
    except google_signin.FlowInvalid:
        raise _flow_invalid() from None
    if isinstance(created, google_signin.Failure):
        raise _generic()
    return _status_out(created)
