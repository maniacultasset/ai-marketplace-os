"""Team invites by copied link (SI5, ADR-0029): create, list and revoke (`team:manage`), the
public lookup and accept steps, and `join` for a signed-in person whose email matches."""

from __future__ import annotations

from typing import Annotated, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.errors import NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.invites import InviteAccept, InviteCreate, InviteTokenIn
from app.api.v1.surface import OPS, route, stub
from app.core.rbac import Principal

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
StatusFilter = Literal["pending", "accepted", "revoked", "expired"]


@route(router, OPS["invites_create"])
@stub
async def invites_create(body: InviteCreate, principal: CurrentPrincipal) -> NoReturn:
    """Invite a teammate (SI5 T5)."""
    raise NotImplementedYet("invites_create")


@route(router, OPS["invites_list"])
@stub
async def invites_list(
    principal: CurrentPrincipal,
    page: Annotated[PageParams, Depends()],
    status: Annotated[StatusFilter | None, Query(description="Only this status.")] = None,
) -> NoReturn:
    """The tenant's invites (SI5 T5)."""
    raise NotImplementedYet("invites_list")


@route(router, OPS["invites_revoke"])
@stub
async def invites_revoke(invite_id: UUID, principal: CurrentPrincipal) -> NoReturn:
    """Revoke an invite (SI5 T5)."""
    raise NotImplementedYet("invites_revoke")


@route(router, OPS["invites_lookup"])
@stub
async def invites_lookup(body: InviteTokenIn, request: Request) -> NoReturn:
    """What an invite link is for (SI5 T6)."""
    raise NotImplementedYet("invites_lookup")


@route(router, OPS["invites_accept"])
@stub
async def invites_accept(body: InviteAccept, request: Request) -> NoReturn:
    """Accept an invite with a new account (SI5 T6, T7)."""
    raise NotImplementedYet("invites_accept")


@route(router, OPS["invites_join"])
@stub
async def invites_join(
    body: InviteTokenIn, request: Request, principal: CurrentPrincipal
) -> NoReturn:
    """Join with the signed-in account (SI5 T6)."""
    raise NotImplementedYet("invites_join")
