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
from app.api.v1.surface import OPS, route
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.rbac import Principal
from app.services import members

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
