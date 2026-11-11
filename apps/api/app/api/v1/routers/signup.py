"""Self-serve sign-up (ADR-0019): the public form and the applicant's status read."""

from __future__ import annotations

from fastapi import APIRouter, Request
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError, password_rules_error
from app.api.rate_limit import (
    enforce_login_limit,
    enforce_signup_limit,
    enforce_signup_poll_limit,
    request_ip,
)
from app.api.v1.schemas.signup import (
    SignupAccepted,
    SignupCreate,
    SignupPollOut,
    SignupPollRequest,
    SignupStatusOut,
    SignupStatusRequest,
)
from app.api.v1.surface import OPS, route
from app.core.errors import NotFoundError
from app.core.password_policy import PolicyError
from app.services import signup

router = APIRouter()


@route(router, OPS["signup_create"])
async def signup_create(body: SignupCreate, request: Request) -> SignupAccepted:
    """Brand name, owner name, email and password (phone and website optional) in; a pending
    request out. Always 202 with this shape: the answer never says whether the email already
    had an account. Re-submitting while a request is pending answers that request. The brand
    logs in only after a platform admin approves (until then the login is 401; read the
    status with `POST /signup/status`). The rest of the brand profile is filled in after
    approval with `PATCH /tenants/me` and `/brand/documents`. The password must be 12+
    characters with an upper-case letter, a lower-case letter, a digit and a symbol, and must
    not contain the owner name or the first part of the email: otherwise 422 with each broken
    rule in `details`. The `request_id` is not stable for an email that already has an account:
    do not store or compare it. `status_handle` reads the request's status with
    `POST /signup/status/poll` (keep it in the browser's storage, never in a URL)."""
    await enforce_signup_limit(request)
    try:
        accepted = await signup.signup(
            email=body.email,
            password=body.password.get_secret_value(),
            brand_name=body.brand_name,
            owner_name=body.owner_name,
            phone=body.phone,
            website=body.website,
            ip=request_ip(request),
        )
    except PolicyError as exc:
        raise password_rules_error([b.message for b in exc.breaches]) from None
    return SignupAccepted(
        request_id=accepted.request_id,
        status=accepted.status,
        status_handle=accepted.status_handle,
    )


@route(router, OPS["signup_status"])
async def signup_status(body: SignupStatusRequest, request: Request) -> SignupStatusOut:
    """The caller's newest sign-up request: `pending` (show "waiting for approval"),
    `rejected` (with the admin's `reason`; the brand may sign up again) or `approved` (log in).
    Every refusal (unknown email, wrong password, no request) is the same 401 as the login.
    A pending answer carries a new `status_handle` for `POST /signup/status/poll`."""
    await enforce_login_limit(request, body.email)
    read = await signup.signup_status(
        body.email, body.password.get_secret_value(), ip=request_ip(request)
    )
    if read is None:
        raise ApiError(401, ErrorCode.UNAUTHENTICATED)
    row = read.row
    return SignupStatusOut.model_validate(
        {
            "request_id": row.id,
            "brand_name": row.brand_name,
            "status": row.status,
            "reason": row.reason,
            "tenant_id": row.created_tenant_id,
            "created_at": row.created_at,
            "decided_at": row.decided_at,
            "status_handle": read.status_handle,
        }
    )


@route(router, OPS["signup_status_poll"])
async def signup_status_poll(body: SignupPollRequest, request: Request) -> SignupPollOut:
    """The status of the one sign-up request a `status_handle` names (the waiting page polls it
    every 15 s while visible): `pending`, `approved` (continue to sign in) or `rejected` (with
    the admin's `reason`). Read only, with no password: it answers only the status, the brand
    name, when it was requested and decided, and the reason. An unknown, replaced or expired
    handle is the same 404. The handle travels only in this body, never in a URL."""
    await enforce_signup_poll_limit(request, body.status_handle)
    found = await signup.poll_status(body.status_handle)
    if found is None:
        raise NotFoundError("status_handle")
    return SignupPollOut(
        status=found.status,
        brand_name=found.brand_name,
        requested_at=found.requested_at,
        decided_at=found.decided_at,
        reason=found.reason if found.status == "rejected" else None,
    )
