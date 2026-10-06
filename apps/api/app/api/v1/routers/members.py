"""Members (owner step S10; PLAN Decision 5): list the tenant's members, change a role or the
active flag."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.errors import NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.schemas.auth import MemberPatch
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


@route(router, OPS["members_list"])
@stub
async def members_list(page: Annotated[PageParams, Depends()]) -> NoReturn:
    """The tenant's members, active or not."""
    raise NotImplementedYet("members_list")


@route(router, OPS["members_patch"])
@stub
async def members_patch(user_id: UUID, body: MemberPatch) -> NoReturn:
    """Change one member's role or active flag."""
    raise NotImplementedYet("members_patch")
