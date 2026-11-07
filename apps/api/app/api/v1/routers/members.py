"""Members (owner step S10; PLAN Decision 5): list the tenant's members, change a role or the
active flag. Both need `team:manage` (owner, admin); only an owner grants or removes owner."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.auth import MemberOut, MemberPage, MemberPatch
from app.api.v1.schemas.invites import ResetLinkOut
from app.api.v1.surface import OPS, route
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.rbac import Principal
from app.services import members
from app.services import password_reset_links as reset_links

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def _out(row: members.MemberRow) -> MemberOut:
    return MemberOut.model_validate(
        {
            "user_id": row.user_id,
            "email": row.email,
            "display_name": row.display_name,
            "role": row.role,
            "is_active": row.is_active,
        }
    )


@route(router, OPS["members_list"])
async def members_list(
    principal: CurrentPrincipal, page: Annotated[PageParams, Depends()]
) -> MemberPage:
    """The tenant's members, active or not, oldest membership first."""
    found = await members.list_members(principal, limit=page.limit, after=page.after)
    last = found.items[-1] if found.items else None
    next_cursor = (
        encode_cursor(last.created_at, last.membership_id) if found.has_more and last else None
    )
    return MemberPage(
        data=[_out(r) for r in found.items],
        page=PageInfo(next_cursor=next_cursor, has_more=found.has_more, limit=page.limit),
    )


@route(router, OPS["members_patch"])
async def members_patch(user_id: UUID, body: MemberPatch, principal: CurrentPrincipal) -> MemberOut:
    """Change one member's role or active flag. A member of another tenant is 404; an admin
    touching an owner, or granting owner, is 403; leaving no active owner is 409
    LAST_OWNER_REQUIRED. Deactivating revokes that member's refresh tokens."""
    try:
        row = await members.patch_member(
            principal, user_id, role=body.role, is_active=body.is_active
        )
    except NotFoundError:
        raise ApiError(404, ErrorCode.NOT_FOUND) from None
    except ForbiddenError:
        raise ApiError(403, ErrorCode.PERMISSION_DENIED) from None
    except ConflictError as exc:
        if exc.code is not ErrorCode.LAST_OWNER_REQUIRED:
            raise
        raise ApiError(409, ErrorCode.LAST_OWNER_REQUIRED) from None
    return _out(row)


@route(router, OPS["members_password_reset"])
async def members_password_reset(user_id: UUID, principal: CurrentPrincipal) -> ResetLinkOut:
    """A password reset link for a member, shown once: copy it to them (24 hours, once; a new
    link replaces the old one). Only for a member of this brand alone, with an authenticator,
    while sign-in codes are required (`MFA_ENFORCEMENT=all`); anyone else, a platform admin
    included, is 403 "Kuinn support resets this account". Only an owner resets an owner (403);
    yourself is 422 (change your own password in Security). Needs a TOTP step-up."""
    try:
        url, expires_at = await reset_links.issue(principal, user_id)
    except reset_links.NotEligibleError:
        raise ApiError(403, ErrorCode.PERMISSION_DENIED, reset_links.NOT_ELIGIBLE_MESSAGE) from None
    except ForbiddenError:
        raise ApiError(403, ErrorCode.PERMISSION_DENIED) from None
    return ResetLinkOut(reset_url=url, expires_at=expires_at)
