"""The brand profile (owner step S16): read it, and update it under `If-Match`."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.v1.deps import IfMatch, get_current_principal
from app.api.v1.schemas.tenants import TenantOut, TenantPatch
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import tenants

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


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
    body: TenantPatch, if_match: IfMatch, principal: CurrentPrincipal, response: Response
) -> TenantOut:
    """Update the brand profile. `If-Match` is the `ETag` from GET; a stale one is 409
    `STALE_VERSION`. A voice change (tone, guidelines, glossary) re-embeds the profile chunks."""
    fields = body.model_dump(include=body.model_fields_set)
    view, tag = await tenants.update_profile(principal, fields, if_match)
    response.headers["ETag"] = tag
    return _out(view)
