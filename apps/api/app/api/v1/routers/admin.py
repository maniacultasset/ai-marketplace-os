"""The platform admin's sign-up queue (ADR-0019): list, approve and reject; and the AI credit
card (SI10): read and Resume. Platform admins only (`users.is_platform_admin`, checked on every
request); everyone else is 403."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.ai_credit import AiCreditOut
from app.api.v1.schemas.signup import (
    SignupApprove,
    SignupReject,
    SignupRequestOut,
    SignupRequestPage,
)
from app.api.v1.surface import OPS, route
from app.core.errors import ConflictError, NotFoundError
from app.core.rbac import Principal
from app.services import ai_credit_admin, signup

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
StatusFilter = Literal["pending", "approved", "rejected"]


def _out(row: signup.SignupRow) -> SignupRequestOut:
    return SignupRequestOut.model_validate(
        {
            "id": row.id,
            "user_id": row.user_id,
            "email": row.email,
            "brand_name": row.brand_name,
            "owner_name": row.owner_name,
            "phone": row.phone,
            "website": row.website,
            "status": row.status,
            "reason": row.reason,
            "tenant_id": row.created_tenant_id,
            "created_at": row.created_at,
            "decided_at": row.decided_at,
            "verified_via": row.verified_via,
        }
    )


@route(router, OPS["admin_signup_requests_list"])
async def admin_signup_requests_list(
    principal: CurrentPrincipal,
    page: Annotated[PageParams, Depends()],
    status: Annotated[
        StatusFilter | None, Query(description="Only this status (`pending` for the queue).")
    ] = None,
) -> SignupRequestPage:
    """Every brand's sign-up requests, oldest first, optionally one status."""
    found = await signup.list_requests(principal, status=status, limit=page.limit, after=page.after)
    last = found.items[-1] if found.items else None
    next_cursor = encode_cursor(last.created_at, last.id) if found.has_more and last else None
    return SignupRequestPage(
        data=[_out(r) for r in found.items],
        page=PageInfo(next_cursor=next_cursor, has_more=found.has_more, limit=page.limit),
    )


def _decision_error(exc: Exception) -> ApiError:
    if isinstance(exc, NotFoundError):
        return ApiError(404, ErrorCode.NOT_FOUND)
    return ApiError(409, ErrorCode.INVALID_STATUS_TRANSITION)


@route(router, OPS["admin_signup_requests_approve"])
async def admin_signup_requests_approve(
    request_id: UUID,
    principal: CurrentPrincipal,
    body: Annotated[SignupApprove | None, Body()] = None,
) -> SignupRequestOut:
    """Create the brand's tenant (status `trial`, `trial_days` default 14, a unique slug from
    the brand name) and its owner membership, exactly as the staff `tenant create` does. The
    owner then logs in normally. Approving an approved request answers it again; a rejected
    one is 409 `INVALID_STATUS_TRANSITION`; an unknown id is 404."""
    try:
        row = await signup.approve(
            principal, request_id, trial_days=body.trial_days if body is not None else None
        )
    except (NotFoundError, ConflictError) as exc:
        if isinstance(exc, ConflictError) and exc.code is not ErrorCode.INVALID_STATUS_TRANSITION:
            raise
        raise _decision_error(exc) from None
    return _out(row)


@route(router, OPS["admin_signup_requests_reject"])
async def admin_signup_requests_reject(
    request_id: UUID, body: SignupReject, principal: CurrentPrincipal
) -> SignupRequestOut:
    """Reject with a reason the applicant reads from `POST /signup/status`. Rejecting a
    rejected request answers it again; an approved one is 409 `INVALID_STATUS_TRANSITION`; an
    unknown id is 404."""
    try:
        row = await signup.reject(principal, request_id, reason=body.reason)
    except (NotFoundError, ConflictError) as exc:
        if isinstance(exc, ConflictError) and exc.code is not ErrorCode.INVALID_STATUS_TRANSITION:
            raise
        raise _decision_error(exc) from None
    return _out(row)


def _usd(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.6f}"


def _credit_out(found: ai_credit_admin.AiCreditView) -> AiCreditOut:
    row = found.credit
    return AiCreditOut(
        state=found.state,
        paused_reason=row.paused_reason,
        paused_at=row.paused_at,
        limit_usd=_usd(row.limit_usd),
        usage_usd=_usd(row.usage_usd),
        remaining_usd=_usd(row.remaining_usd),
        floor_usd=f"{found.floor_usd:.6f}",
        checked_at=row.checked_at,
        today_spend_usd=f"{found.today_spend_usd:.6f}",
        daily_cap_usd=f"{found.daily_cap_usd:.6f}",
    )


@route(router, OPS["admin_ai_credit_get"])
async def admin_ai_credit_get(principal: CurrentPrincipal) -> AiCreditOut:
    """The AI credit: the pause, the last key check, the floor and today's platform spend."""
    return _credit_out(await ai_credit_admin.view())


@route(router, OPS["admin_ai_credit_resume"])
async def admin_ai_credit_resume(principal: CurrentPrincipal) -> AiCreditOut:
    """Clear the AI pause now (after a top-up or a key fix). Repeatable; the credit floor still
    applies at the next check."""
    return _credit_out(await ai_credit_admin.resume(principal))
