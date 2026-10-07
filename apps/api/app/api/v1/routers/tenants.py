"""Tenant stubs (owner step S16)."""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter

from app.api.errors import NotImplementedYet
from app.api.v1.deps import IfMatch
from app.api.v1.schemas.tenants import TenantPatch
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


@route(router, OPS["tenants_me"])
@stub
async def tenants_me() -> NoReturn:
    """The brand profile of the caller's tenant (tone, guidelines, glossary)."""
    raise NotImplementedYet("tenants_me")


@route(router, OPS["tenants_me_update"])
@stub
async def tenants_me_update(body: TenantPatch, if_match: IfMatch) -> NoReturn:
    """Update the brand profile; a voice change re-embeds the profile chunks."""
    raise NotImplementedYet("tenants_me_update")
