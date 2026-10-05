"""Import stubs (owner step S13)."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Path as PathParam

from app.api.errors import NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.deps import IdempotencyKey
from app.api.v1.schemas.imports import ImportRowStatus
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


@route(router, OPS["imports_get"])
@stub
async def imports_get(import_id: Annotated[UUID, PathParam()]) -> NoReturn:
    """The import report: counts and the import-level error. `rows` is always empty here; read
    rows from `/imports/{import_id}/rows`."""
    raise NotImplementedYet("imports_get")


@route(router, OPS["imports_rows"])
@stub
async def imports_rows(
    import_id: Annotated[UUID, PathParam()],
    page: Annotated[PageParams, Depends()],
    status: Annotated[ImportRowStatus | None, Query()] = None,
) -> NoReturn:
    """The import's rows, optionally only `imported` or `failed` ones."""
    raise NotImplementedYet("imports_rows")


@route(router, OPS["imports_rows_retry"])
@stub
async def imports_rows_retry(
    import_id: Annotated[UUID, PathParam()], idempotency_key: IdempotencyKey
) -> NoReturn:
    """Re-run every failed row of the import, and only those."""
    raise NotImplementedYet("imports_rows_retry")
