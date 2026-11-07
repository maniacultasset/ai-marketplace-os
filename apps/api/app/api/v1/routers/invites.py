"""Team invites by copied link (SI5, ADR-0029): create, list and revoke (`team:manage`), the
public lookup and accept steps, and `join` for a signed-in person whose email matches."""

from __future__ import annotations

from typing import Annotated, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError, NotImplementedYet
from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.invites import (
    InviteAccept,
    InviteCreate,
    InviteCreated,
    InviteOut,
    InvitePage,
    InviteTokenIn,
)
from app.api.v1.surface import OPS, route, stub
from app.core.errors import ForbiddenError
from app.core.rbac import Principal
from app.services import invites

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
StatusFilter = Literal["pending", "accepted", "revoked", "expired"]


def _out(row: invites.InviteRow) -> InviteOut:
    return InviteOut.model_validate(
        {
            "id": row.id,
            "email": row.email,
            "role": row.role,
            "status": row.status,
            "invited_by_user_id": row.invited_by_user_id,
            "expires_at": row.expires_at,
            "created_at": row.created_at,
            "accepted_at": row.accepted_at,
            "revoked_at": row.revoked_at,
        }
    )


def _forbidden() -> ApiError:
    return ApiError(403, ErrorCode.PERMISSION_DENIED)


@route(router, OPS["invites_create"])
async def invites_create(body: InviteCreate, principal: CurrentPrincipal) -> InviteCreated:
    """Invite a teammate by email and role. The answer holds `invite_url` once: copy it to the
    person (it works once, for 7 days). Only an owner invites an owner (403). The email of a
    member here, active or not, is 422 at `email`, and so is a 51st pending invite. A new invite
    for an email with an open one revokes the old link. Needs a recent step-up under
    `MFA_ENFORCEMENT=all`."""
    try:
        row, url = await invites.create_invite(principal, email=body.email, role=body.role)
    except ForbiddenError:
        raise _forbidden() from None
    return InviteCreated(invite=_out(row), invite_url=url)


@route(router, OPS["invites_list"])
async def invites_list(
    principal: CurrentPrincipal,
    page: Annotated[PageParams, Depends()],
    status: Annotated[StatusFilter | None, Query(description="Only this status.")] = None,
) -> InvitePage:
    """The tenant's invites, newest first; never a token or its hash."""
    found = await invites.list_invites(principal, status=status, limit=page.limit, after=page.after)
    last = found.items[-1] if found.items else None
    next_cursor = encode_cursor(last.created_at, last.id) if found.has_more and last else None
    return InvitePage(
        data=[_out(r) for r in found.items],
        page=PageInfo(next_cursor=next_cursor, has_more=found.has_more, limit=page.limit),
    )


@route(router, OPS["invites_revoke"])
async def invites_revoke(invite_id: UUID, principal: CurrentPrincipal) -> InviteOut:
    """Revoke a pending or expired invite; revoking it again answers the same row. An accepted
    invite is 409; an owner invite is revoked only by an owner (403); another tenant's is 404."""
    try:
        row = await invites.revoke_invite(principal, invite_id)
    except ForbiddenError:
        raise _forbidden() from None
    return _out(row)


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
