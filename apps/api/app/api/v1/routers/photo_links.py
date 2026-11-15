"""Photos from spreadsheet links (G2): retry the links that failed, and hide one the seller gave
up on. Writes need `product:import` (owner, operator), as image uploads do. Another tenant's
product, import or link is 404, never 403. While `SHEET_PHOTOS_ENABLED` is off the retries
answer 501 `NOT_IMPLEMENTED`. Routes import no `sqlalchemy` or `app.db`."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.jobs import JobAccepted

from app.api.errors import ApiError
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.photo_links import PhotoLinksRetryOut
from app.api.v1.surface import OPS, route
from app.core.errors import ConflictError
from app.core.rbac import Principal
from app.services import photo_links

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
_CONFLICTS = (ErrorCode.INVALID_STATUS_TRANSITION, ErrorCode.IDEMPOTENCY_KEY_REUSED)


def _off() -> ApiError:
    return ApiError(501, ErrorCode.NOT_IMPLEMENTED, photo_links.SheetPhotosOff.MESSAGE)


@route(router, OPS["products_photo_links_retry"])
async def products_photo_links_retry(
    product_id: Annotated[UUID, PathParam()],
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> JobAccepted:
    """Download again the product's photo links that failed (and those the per-file cap
    skipped). The results show on the product (`photo_links`) as the job runs; poll the product
    or `poll_url`. 409 when no link can be retried."""
    try:
        body = await photo_links.retry_product(
            principal, product_id, idempotency_key, request.url.path
        )
    except photo_links.SheetPhotosOff:
        raise _off() from None
    except ConflictError as exc:
        if exc.code not in _CONFLICTS:
            raise
        raise ApiError(409, exc.code) from None
    return JobAccepted.model_validate(body)


@route(router, OPS["imports_photo_links_retry"])
async def imports_photo_links_retry(
    import_id: Annotated[UUID, PathParam()],
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> PhotoLinksRetryOut:
    """Download again every photo link of the import that failed (and those the per-file cap
    skipped), one job per product. The rows' `photos` show the results. 409 when no link can be
    retried."""
    try:
        body = await photo_links.retry_import(
            principal, import_id, idempotency_key, request.url.path
        )
    except photo_links.SheetPhotosOff:
        raise _off() from None
    except ConflictError as exc:
        if exc.code not in _CONFLICTS:
            raise
        raise ApiError(409, exc.code) from None
    return PhotoLinksRetryOut.model_validate(body)


@route(router, OPS["photo_links_dismiss"])
async def photo_links_dismiss(
    link_id: Annotated[UUID, PathParam()], principal: CurrentPrincipal
) -> Response:
    """Hide a failed or skipped photo link from the product and the import report. 409 for a
    link that is waiting, downloading or saved."""
    try:
        await photo_links.dismiss(principal, link_id)
    except ConflictError as exc:
        if exc.code is not ErrorCode.INVALID_STATUS_TRANSITION:
            raise
        raise ApiError(409, exc.code) from None
    return Response(status_code=204)
