"""Issue inbox routes (owner step S19)."""

from __future__ import annotations

from typing import Annotated, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace

from app.api.errors import NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.deps import IdempotencyKey
from app.api.v1.surface import OPS, route, stub

router = APIRouter()
IssueId = Annotated[UUID, PathParam()]


@route(router, OPS["issues_list"])
@stub
async def issues_list(
    page: Annotated[PageParams, Depends()],
    status: Annotated[Literal["open", "auto_fixed", "resolved", "dismissed"], Query()] = "open",
    listing_id: Annotated[UUID | None, Query()] = None,
    source: Annotated[ErrorSource | None, Query()] = None,
    error_code: Annotated[ErrorCode | None, Query()] = None,
    marketplace: Annotated[Marketplace | None, Query()] = None,
) -> NoReturn:
    """The brand's issues, newest first; open ones unless `status` says otherwise."""
    raise NotImplementedYet("issues_list")


@route(router, OPS["issues_dismiss"])
@stub
async def issues_dismiss(issue_id: IssueId, idempotency_key: IdempotencyKey) -> NoReturn:
    """Accept an open warning (reviewer or owner). An error is never dismissed: the Rules
    Engine stays the arbiter (409 INVALID_STATUS_TRANSITION)."""
    raise NotImplementedYet("issues_dismiss")
