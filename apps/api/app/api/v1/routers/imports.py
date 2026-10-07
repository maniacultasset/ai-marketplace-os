"""Imports (owner step S13): the import report, its rows (keyset on the row number, opaque
cursor), and the rows retry. Another tenant's import is 404, never 403."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.imports import ImportReport

from app.api.errors import ApiError, NotImplementedYet
from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.imports import ImportRowPage, ImportRowStatus
from app.api.v1.surface import OPS, route, stub
from app.core.rbac import Principal
from app.services.imports import reports

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["imports_get"])
async def imports_get(
    import_id: Annotated[UUID, PathParam()], principal: CurrentPrincipal
) -> ImportReport:
    """The import report: counts and the import-level error. `rows` is always empty here; read
    rows from `/imports/{import_id}/rows`."""
    return await reports.get_report(principal, import_id)


@route(router, OPS["imports_rows"])
async def imports_rows(
    import_id: Annotated[UUID, PathParam()],
    page: Annotated[PageParams, Depends()],
    principal: CurrentPrincipal,
    status: Annotated[ImportRowStatus | None, Query()] = None,
) -> ImportRowPage:
    """The import's rows, optionally only `imported` or `failed` ones."""
    after = page.after[1] if page.after is not None else None
    found = await reports.rows_page(
        principal, import_id, status=status, after_row=after, limit=page.limit
    )
    if found is None:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_FAILED,
            details=[{"loc": ["query", "cursor"], "msg": "Invalid cursor."}],
        )
    last = found.last
    next_cursor = encode_cursor(last.created_at, last.id) if found.has_more and last else None
    return ImportRowPage(
        data=found.rows,
        page=PageInfo(next_cursor=next_cursor, has_more=found.has_more, limit=page.limit),
    )


@route(router, OPS["imports_rows_retry"])
@stub
async def imports_rows_retry(
    import_id: Annotated[UUID, PathParam()], idempotency_key: IdempotencyKey
) -> NoReturn:
    """Re-run every failed row of the import, and only those."""
    raise NotImplementedYet("imports_rows_retry")
