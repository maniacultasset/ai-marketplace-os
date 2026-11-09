"""Spreadsheet column maps (owner step SI14): map a seller's header row onto the importer's
accepted names, then confirm it before `POST /products/bulk` uses it with `column_map_id`.
Create and confirm need `product:import` (owner, operator); any member reads. Another tenant's map
is 404, never 403. Routes import no `sqlalchemy` or `app.db`."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.column_maps import ColumnMapConfirm, ColumnMapCreate, ColumnMapOut
from app.api.v1.surface import OPS, route
from app.core.errors import ConflictError
from app.core.rbac import Principal
from app.services.imports import column_maps

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def _reused(exc: ConflictError) -> ApiError:
    return ApiError(409, exc.code)


@route(router, OPS["imports_column_map_create"])
async def imports_column_map_create(
    body: ColumnMapCreate,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
    response: Response,
) -> ColumnMapOut:
    """Map a header row. Exact names, aliases and close spellings map first; 201 `ready` when
    nothing is left, or when the AI step is off or AI is paused (the leftovers wait for you). 202
    `mapping` while the AI maps the leftovers (poll the map). Sample cells are checked but
    not stored and never sent to a model. A repeated header
    is 422."""
    try:
        status, out = await column_maps.create_map(
            principal, body.headers, body.samples, idempotency_key, request.url.path
        )
    except ConflictError as exc:
        if exc.code is not ErrorCode.IDEMPOTENCY_KEY_REUSED:
            raise
        raise _reused(exc) from None
    response.status_code = status
    return ColumnMapOut.model_validate(out)


@route(router, OPS["imports_column_map_get"])
async def imports_column_map_get(
    map_id: Annotated[UUID, PathParam()], principal: CurrentPrincipal
) -> ColumnMapOut:
    """One map: each column by position (`header_shown` is the header when it is a plain name,
    else `column N`), its key, how it was mapped and whether it needs your answer."""
    return ColumnMapOut.model_validate(await column_maps.get_map(principal, map_id))


@route(router, OPS["imports_column_map_confirm"])
async def imports_column_map_confirm(
    map_id: Annotated[UUID, PathParam()],
    body: ColumnMapConfirm,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> ColumnMapOut:
    """Confirm the map with an answer per column position (a key, or null to skip the column).
    Every column that needs review must be answered; two columns cannot share a key. 409 while
    the AI is still mapping or once confirmed; 422 `IMPORT_FAILED` when the result lacks a
    required column (`seller_sku`, `product_name`, `category`)."""
    answers = [answer.model_dump() for answer in body.mappings]
    try:
        _status, out = await column_maps.confirm_map(
            principal, map_id, answers, idempotency_key, request.url.path
        )
    except column_maps.ColumnMapRefused as exc:
        raise ApiError(422, ErrorCode.IMPORT_FAILED, report=exc.report) from None
    return ColumnMapOut.model_validate(out)
