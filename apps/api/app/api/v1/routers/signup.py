"""Self-serve sign-up (ADR-0019): the public form and the applicant's status read."""

from __future__ import annotations

from fastapi import APIRouter, Request
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.rate_limit import enforce_login_limit, enforce_signup_limit, request_ip
from app.api.v1.schemas.signup import (
    SignupAccepted,
    SignupCreate,
    SignupStatusOut,
    SignupStatusRequest,
)
from app.api.v1.surface import OPS, route
from app.services import signup

router = APIRouter()


@route(router, OPS["signup_create"])
async def signup_create(body: SignupCreate, request: Request) -> SignupAccepted:
    """Brand name, owner name, email and password (phone and website optional) in; a pending
    request out. Always 202 with this shape: the answer never says whether the email already
    had an account. Re-submitting while a request is pending answers that request. The brand
    logs in only after a platform admin approves (until then the login is 401; read the
    status with `POST /signup/status`). The rest of the brand profile is filled in after
    approval with `PATCH /tenants/me` and `/brand/documents`."""
    await enforce_signup_limit(request)
    accepted = await signup.signup(
        email=body.email,
        password=body.password.get_secret_value(),
        brand_name=body.brand_name,
        owner_name=body.owner_name,
        phone=body.phone,
        website=body.website,
        ip=request_ip(request),
    )
    return SignupAccepted(request_id=accepted.request_id, status=accepted.status)


@route(router, OPS["signup_status"])
async def signup_status(body: SignupStatusRequest, request: Request) -> SignupStatusOut:
    """The caller's newest sign-up request: `pending` (show "waiting for approval"),
    `rejected` (with the admin's `reason`; the brand may sign up again) or `approved` (log in).
    Every refusal (unknown email, wrong password, no request) is the same 401 as the login."""
    await enforce_login_limit(request, body.email)
    row = await signup.signup_status(body.email, body.password.get_secret_value())
    if row is None:
        raise ApiError(401, ErrorCode.UNAUTHENTICATED)
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
