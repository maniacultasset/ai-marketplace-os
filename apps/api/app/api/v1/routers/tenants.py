"""The brand profile (owner step S16): read it, and update it under `If-Match`."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from mkos_schemas.auth import JWTClaims
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace

from app.api.errors import ApiError
from app.api.v1.deps import IfMatch, access_claims, check_recent_mfa, get_current_principal
from app.api.v1.schemas.tenants import TenantCapabilitiesOut, TenantOut, TenantPatch
from app.api.v1.surface import OPS, route
from app.core.config import get_settings
from app.core.errors import ForbiddenError
from app.core.flags import publish_enabled
from app.core.rbac import Principal
from app.services import tenants

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
Claims = Annotated[JWTClaims, Depends(access_claims)]


def capabilities_out() -> TenantCapabilitiesOut:
    """The deployment's switches, read the way the services read them."""
    s = get_settings()
    return TenantCapabilitiesOut(
        amazon_publishing_enabled=publish_enabled(Marketplace.AMAZON, s),
        category_suggest_enabled=s.category_suggest_enabled,
        column_map_ai_enabled=s.column_map_ai_enabled,
        photo_qa_enabled=s.photo_qa_enabled,
        category_lamps_enabled=s.category_lamps_enabled,
        size_chart_read_enabled=s.size_chart_read_enabled,
        image_edit_enabled=s.image_edit_enabled,
    )


def _out(view: tenants.TenantView) -> TenantOut:
    return TenantOut.model_validate(
        {
            "id": view.id,
            "name": view.name,
            "slug": view.slug,
            "tone": view.tone,
            "guidelines": view.guidelines,
            "glossary": view.glossary,
            "status": view.status,
            "trial_ends_at": view.trial_ends_at,
            "capabilities": capabilities_out(),
            "owner_self_approval": view.owner_self_approval,
        }
    )


@route(router, OPS["tenants_me"])
async def tenants_me(principal: CurrentPrincipal, response: Response) -> TenantOut:
    """The brand profile of the caller's tenant (tone, guidelines, glossary), with its `ETag`."""
    view, tag = await tenants.get_profile(principal)
    response.headers["ETag"] = tag
    return _out(view)


@route(router, OPS["tenants_me_update"])
async def tenants_me_update(
    body: TenantPatch,
    if_match: IfMatch,
    principal: CurrentPrincipal,
    claims: Claims,
    response: Response,
) -> TenantOut:
    """Update the brand profile. `If-Match` is the `ETag` from GET; a stale one is 409
    `STALE_VERSION`. A voice change (tone, guidelines, glossary) re-embeds the profile chunks.
    `owner_self_approval` (ADR-0031) needs the owner role (else 403 `PERMISSION_DENIED`), then a
    step-up like a team change (403 `MFA_STEP_UP_REQUIRED` / `MFA_ENROLLMENT_REQUIRED`)."""
    fields = body.model_dump(include=body.model_fields_set)
    if tenants.SETTING_FIELD in fields:
        if principal.role not in tenants.SETTING_ROLES:
            raise ApiError(403, ErrorCode.PERMISSION_DENIED)
        await check_recent_mfa(claims, principal, otp_only=False)
    try:
        view, tag = await tenants.update_profile(principal, fields, if_match)
    except ForbiddenError:  # demoted or deactivated since the request's probe
        raise ApiError(403, ErrorCode.PERMISSION_DENIED) from None
    response.headers["ETag"] = tag
    return _out(view)
