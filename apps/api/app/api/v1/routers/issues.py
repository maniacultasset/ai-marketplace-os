"""Issue inbox routes (owner step S19)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace

from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.issues import IssueOut, IssuePage
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.repositories.listing_issues import IssueFilter
from app.services import issues

router = APIRouter()
IssueId = Annotated[UUID, PathParam()]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["issues_list"])
async def issues_list(
    page: Annotated[PageParams, Depends()],
    principal: CurrentPrincipal,
    status: Annotated[Literal["open", "auto_fixed", "resolved", "dismissed"], Query()] = "open",
    listing_id: Annotated[UUID | None, Query()] = None,
    source: Annotated[ErrorSource | None, Query()] = None,
    error_code: Annotated[ErrorCode | None, Query()] = None,
    marketplace: Annotated[Marketplace | None, Query()] = None,
) -> IssuePage:
    """The brand's issues, newest first; open ones unless `status` says otherwise."""
    filters = IssueFilter(
        status=status,
        listing_id=listing_id,
        source=None if source is None else ErrorSource(source).value,
        error_code=None if error_code is None else ErrorCode(error_code).value,
        marketplace=None if marketplace is None else Marketplace(marketplace).value,
    )
    rows, more, key = await issues.list_issues(
        principal, filters, after=page.after, limit=page.limit
    )
    return IssuePage(
        data=[IssueOut.model_validate(r) for r in rows],
        page=PageInfo(
            next_cursor=None if key is None else encode_cursor(*key),
            has_more=more,
            limit=page.limit,
        ),
    )


@route(router, OPS["issues_dismiss"])
async def issues_dismiss(
    issue_id: IssueId,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> IssueOut:
    """Accept an open warning (reviewer or owner). An error is never dismissed: the Rules
    Engine stays the arbiter (409 INVALID_STATUS_TRANSITION)."""
    stored = await issues.accept_dismiss(principal, idempotency_key, request.url.path, issue_id)
    return IssueOut.model_validate(stored)
